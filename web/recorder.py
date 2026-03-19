"""
Playwright-based demo recorder.
Manages browser sessions, controlled via WebSocket from app.py.
Output matches the agent run format: traj.jsonl + step_N_timestamp.png
"""

import base64
import json
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright


def _timestamp() -> str:
    """Generate timestamp in the same format as agent runs: YYYYMMDD@HHMMSSmmm"""
    now = datetime.now()
    return now.strftime("%Y%m%d@%H%M%S") + f"{now.microsecond // 1000:03d}"


class DemoSession:
    """
    One recording session = one Playwright browser instance.
    Outputs in the same format as agent runs (traj.jsonl + step_N_timestamp.png).
    """

    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.task_dir = None
        self.step_count = 0
        self.task_id = ""
        self.annotator = ""
        self.start_url = ""
        self._traj_file = None
        self._start_time = None
        self._step_times = []  # elapsed seconds at each step

    async def start(self, url: str, task_id: str, annotator: str,
                    demos_dir: Path, viewport: tuple = (1280, 720)) -> str:
        """
        Launch Chrome, navigate to url, take initial screenshot.
        Creates output dir: demos_dir/{annotator}/{task_id}/
        Returns initial screenshot as base64 string.
        """
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=False,
            channel="chrome",
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self.context = await self.browser.new_context(
            viewport={"width": viewport[0], "height": viewport[1]},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        self.page = await self.context.new_page()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        # Let the page render before taking the first screenshot
        await self.page.wait_for_timeout(2000)

        # Create output directory (flat, like agent runs)
        self.task_dir = demos_dir / annotator / task_id
        self.task_dir.mkdir(parents=True, exist_ok=True)

        # Internal state
        self.step_count = 0
        self.task_id = task_id
        self.annotator = annotator
        self.start_url = url
        self._start_time = time.monotonic()
        self._step_times = []

        # Open traj.jsonl for appending
        self._traj_file = open(self.task_dir / "traj.jsonl", "w")

        # Write initial metadata (updated progressively after each step)
        self._write_metadata(in_progress=True)

        # Initial screenshot (step 0)
        screenshot_b64 = await self._take_screenshot()
        ts = _timestamp()
        screenshot_file = f"step_0_{ts}.png"
        self._save_screenshot(screenshot_b64, screenshot_file)
        self._write_traj_line(
            step_num=0,
            timestamp=ts,
            action=None,
            screenshot_file=screenshot_file,
        )
        return screenshot_b64

    async def execute_action(self, action: dict) -> str:
        """
        Execute action via Playwright, take post-action screenshot.
        Returns new screenshot as base64.
        """
        action_type = action["type"]

        if action_type == "click":
            await self.page.mouse.click(action["x"], action["y"])
        elif action_type == "type":
            if "selector" in action:
                await self.page.fill(action["selector"], action["text"])
            else:
                await self.page.keyboard.type(action["text"])
        elif action_type == "scroll":
            dx = action.get("dx", 0)
            dy = action.get("dy", 0)
            await self.page.mouse.wheel(dx, dy)
        elif action_type == "goto":
            goto_url = action["url"]
            if not goto_url.startswith(("http://", "https://")):
                goto_url = "https://" + goto_url
            await self.page.goto(goto_url, wait_until="domcontentloaded", timeout=60000)
        elif action_type == "key_press":
            await self.page.keyboard.press(action["key"])
        elif action_type == "hover":
            await self.page.mouse.move(action["x"], action["y"])
        elif action_type == "select":
            await self.page.select_option(action["selector"], action["value"])
        elif action_type == "terminate":
            # No browser action — just record the final step
            pass

        # Wait for page to settle
        if action_type != "terminate":
            await self.page.wait_for_timeout(500)

        self.step_count += 1
        screenshot_b64 = await self._take_screenshot()
        ts = _timestamp()
        screenshot_file = f"step_{self.step_count}_{ts}.png"
        self._save_screenshot(screenshot_b64, screenshot_file)

        # Map our action format to agent-compatible action structure
        agent_action = self._to_agent_action(action)

        is_done = action_type == "terminate"
        self._write_traj_line(
            step_num=self.step_count,
            timestamp=ts,
            action=agent_action,
            screenshot_file=screenshot_file,
            response=action.get("response", "") if is_done else "",
            done=is_done,
        )
        return screenshot_b64

    def _write_metadata(self, in_progress: bool = False,
                         success: bool = False, answer: str = None):
        """Write run_metadata.json. Called after every step for crash recovery."""
        if not self.task_dir:
            return
        total_seconds = round(time.monotonic() - self._start_time, 2) if self._start_time else 0
        (self.task_dir / "run_metadata.json").write_text(json.dumps({
            "source": "human_demo",
            "annotator": self.annotator,
            "task_id": self.task_id,
            "success": success,
            "answer": answer,
            "step_count": self.step_count,
            "start_url": self.start_url,
            "duration_seconds": total_seconds,
            "step_times": self._step_times,
            "in_progress": in_progress,
            "timestamp": datetime.now().isoformat(),
        }, indent=2))

    async def stop(self, success: bool, answer: str = None) -> Path:
        """
        Finalize recording. Write result.txt and run_metadata.json.
        Close browser. Returns output directory path.
        """
        # Close traj file
        if self._traj_file:
            self._traj_file.close()
            self._traj_file = None

        # result.txt (matching agent format)
        (self.task_dir / "result.txt").write_text(
            "1.0" if success else "0.0"
        )

        # Final metadata (in_progress=False)
        self._write_metadata(in_progress=False, success=success, answer=answer)

        await self.close()
        return self.task_dir

    async def close(self):
        """Cleanup browser resources."""
        if self._traj_file:
            try:
                self._traj_file.close()
            except Exception:
                pass
            self._traj_file = None
        if hasattr(self, "browser") and self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None
        if hasattr(self, "playwright") and self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
            self.playwright = None

    async def _take_screenshot(self) -> str:
        png_bytes = await self.page.screenshot(type="png")
        return base64.b64encode(png_bytes).decode()

    def _save_screenshot(self, screenshot_b64: str, filename: str):
        """Save screenshot as PNG file in task_dir (flat, like agent runs)."""
        (self.task_dir / filename).write_bytes(
            base64.b64decode(screenshot_b64)
        )

    def _to_agent_action(self, action: dict) -> dict:
        """Convert our UI action format to agent-compatible action structure."""
        action_type = action["type"]
        agent_input = {}

        if action_type == "click":
            agent_input = {
                "action": "left_click",
                "coordinate": [action["x"], action["y"]],
            }
        elif action_type == "type":
            agent_input = {
                "action": "type",
                "text": action.get("text", ""),
            }
        elif action_type == "scroll":
            agent_input = {
                "action": "scroll",
                "coordinate": [action.get("x", 640), action.get("y", 360)],
                "direction": "down" if action.get("dy", 0) > 0 else "up",
            }
        elif action_type == "goto":
            agent_input = {
                "action": "goto",
                "url": action.get("url", ""),
            }
        elif action_type == "key_press":
            agent_input = {
                "action": "key",
                "text": action.get("key", ""),
            }
        elif action_type == "hover":
            agent_input = {
                "action": "move",
                "coordinate": [action["x"], action["y"]],
            }
        elif action_type == "select":
            agent_input = {
                "action": "select",
                "selector": action.get("selector", ""),
                "value": action.get("value", ""),
            }
        elif action_type == "terminate":
            agent_input = {
                "action": "terminate",
                "response": action.get("response", ""),
            }

        return {
            "name": "computer",
            "input": agent_input,
            "action_type": "human",
        }

    def _write_traj_line(self, step_num: int, timestamp: str,
                         action: dict | None, screenshot_file: str,
                         response: str = "", done: bool = False):
        """Write one line to traj.jsonl in agent-compatible format."""
        elapsed = round(time.monotonic() - self._start_time, 2) if self._start_time else 0
        self._step_times.append(elapsed)
        entry = {
            "step_num": step_num,
            "action_timestamp": timestamp,
            "action": action if action else {},
            "response": response,
            "reward": 0,
            "done": done,
            "info": {},
            "screenshot_file": screenshot_file,
            "elapsed_seconds": elapsed,
        }
        self._traj_file.write(json.dumps(entry) + "\n")
        self._traj_file.flush()

        # Update metadata on disk after every step (crash recovery)
        self._write_metadata(in_progress=True)

    @property
    def elapsed_seconds(self) -> float:
        if self._start_time is None:
            return 0
        return round(time.monotonic() - self._start_time, 2)

    @property
    def current_url(self) -> str:
        return self.page.url if self.page else ""

    @property
    def page_title(self) -> str:
        if self.page:
            # Can't call async title() from property, return empty
            return ""
        return ""
