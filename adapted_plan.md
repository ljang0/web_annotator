# Plan: Standalone Web Trajectory Viewer, Annotator & Demo Recorder

## Context

We have downloaded trajectory data on a **local machine**. The exact directory structure and file formats are not known in advance — the implementing agent inspects the data on disk before writing discovery/loading code. We're building a **self-contained** web app (no Streamlit, no external backend dependencies) that lets us browse, annotate, record new human demos, and analyze trajectories — all through `localhost`.

All discovery, loading, and storage logic is **built from scratch** inside the web app — no imports from any annotation framework.

---

## No Hardcoded Layout Assumptions

The trajectory data structure is **not specified in advance**. Before writing any discovery or loading code, the implementing agent **must inspect the actual data on disk** to understand the layout:

### Required: Pre-implementation data inspection

1. **Tree the results directory** — run `find $RESULTS_DIR -maxdepth 4 -type f | head -80` (or similar) to see the actual nesting, filenames, and extensions present.
2. **Sample representative files** — read 2–3 JSON files (whichever exist: metadata files, trajectory files, per-step files) to understand their schema, key names, and data types. Don't assume key names like `"action"` or `"observation"` — read them.
3. **Check for images** — confirm what screenshot format is used (`.png`, `.jpg`, `.webp`), where they live, and how they relate to steps.
4. **Note variations** — if the tree shows multiple benchmarks or models with slightly different structures, sample from each.

### What the discovery code must handle

Based on what's found on disk, the discovery module should:

- **Detect "run" directories** — use whatever signals are actually present (screenshot files, JSON files with trajectory-like content, numbered subdirectories, etc.) rather than checking for hardcoded filenames.
- **Extract metadata opportunistically** — read whatever metadata files exist, fall back to parsing directory path segments, fall back to "unknown". Never crash on a missing file.
- **Build a step sequence from whatever's there** — could be a single trajectory JSON, could be per-step files in subdirectories, could be a flat directory of numbered screenshots with a sidecar JSON. The loader adapts to what it finds.
- **Resolve screenshot paths** — however screenshots are stored, produce a URL path that the static file mount can serve.

The key principle: **the code adapts to the data, not the other way around.** The implementing agent reads the disk first and writes discovery logic that matches the actual structure — not a guessed or assumed one.

---

## Architecture

- **Backend:** FastAPI + uvicorn (one process, one port)
- **Frontend:** Single HTML file with vanilla JS/CSS, dark theme, four tabs
- **Demo recording:** Playwright headless Chromium controlled via WebSocket, screenshots streamed as base64
- **Data:** Read trajectory dirs from disk; SQLite for annotations/reviews
- **Access:** Run locally, open `localhost:8000`

---

## Files to Create

### 1. `web/app.py` — FastAPI server (entry point)

```
python web/app.py [--port 8000] [--results-dir ./results] [--annotations-dir ./annotations] [--demos-dir ./human_demos]
```

**Self-contained backend modules** (can be inline or split into `web/discovery.py`, `web/storage.py`):

#### Discovery (`discover_runs`, `load_trajectory`)

- **Before writing this module:** inspect the actual contents of `results_dir` and `demos_dir` on disk (tree listing + sample JSON files) to determine the real directory layout, file naming conventions, and JSON schemas.
- Walk both directories recursively, detecting "run" directories based on signals actually found during inspection (e.g., presence of screenshot files, trajectory-like JSON, numbered subdirectories — whatever the data actually uses).
- For each discovered run, extract metadata from whatever files/paths are available:
  - `run_id`: relative path from its root dir
  - `source`: `"agent"` (from results_dir) or `"human_demo"` (from demos_dir)
  - Any other metadata (benchmark, model, task_id, step_count, intent, score) — read from files if present, parse from path segments if not, default to "unknown"
- Cache the scan result; re-scan on explicit refresh or after N minutes

#### Trajectory loading

- Given a `run_id` (relative path) and `source`, resolve the absolute path and load the full step sequence.
- The loading strategy depends on what was found during disk inspection — could be reading a single trajectory JSON, iterating per-step subdirectories, parsing a flat directory of screenshots + sidecar files, etc.
- Normalize into a common internal format per step: `{ step: int, screenshot_path: str, action: dict|null, observation: dict|null, reasoning: str|null }`
- `screenshot_path` is the URL path for static serving

#### Annotation storage (SQLite)

- `init_db()` → create `annotations.db` in `annotations_dir` with tables:
  - `step_annotations(run_id, task_id, step, verdict, correction_json, annotator, timestamp)`
  - `task_annotations(run_id, task_id, success, failure_mode, reasoning, rubric_json, annotator, duration_seconds, timestamp)`
- `save_step_annotation(...)`, `save_task_annotation(...)`
- `get_annotations(run_id, task_id)` → returns existing annotations for pre-fill
- `get_progress()` → returns counts for dashboard

#### REST endpoints

| Endpoint | Description |
|---|---|
| `GET /api/runs` | Scan and return all discovered runs (agent + demos), grouped by benchmark/model. Each entry: run_id, source, benchmark, model, task_id, step_count, score, annotation_status |
| `GET /api/runs/refresh` | Force re-scan of results + demos directories |
| `GET /api/trajectory/{run_id:path}?source=agent` | Load full step array for a run. `source` param selects results_dir vs demos_dir |
| `GET /api/annotations/{run_id:path}` | Get existing annotations for a run |
| `POST /api/annotations` | Save annotation (step-level or task-level) |
| `GET /api/dashboard` | Annotation progress, per-model pass rates, failure mode distribution |
| `GET /api/export` | Export annotations as JSONL |

#### WebSocket endpoint

| Endpoint | Description |
|---|---|
| `WS /ws/record` | Demo recording session — bridges frontend to `DemoSession` in `recorder.py` |

WebSocket handler:
```python
@app.websocket("/ws/record")
async def ws_record(ws: WebSocket):
    await ws.accept()
    session = None
    try:
        while True:
            msg = await ws.receive_json()
            if msg["type"] == "start":
                session = DemoSession()
                screenshot_b64 = await session.start(
                    url=msg["url"],
                    task_id=msg["task_id"],
                    annotator=msg["annotator"],
                    demos_dir=DEMOS_DIR,
                )
                await ws.send_json({
                    "type": "recording_started",
                    "step": 0,
                    "image_b64": screenshot_b64,
                    "url": msg["url"],
                })
            elif msg["type"] == "action":
                screenshot_b64 = await session.execute_action(msg["action"])
                await ws.send_json({
                    "type": "screenshot",
                    "step": session.step_count,
                    "image_b64": screenshot_b64,
                    "url": session.current_url,
                    "title": session.page_title,
                })
            elif msg["type"] == "stop":
                task_dir = await session.stop(
                    success=msg.get("success", False),
                    answer=msg.get("answer"),
                )
                await ws.send_json({
                    "type": "recording_stopped",
                    "task_dir": str(task_dir),
                })
                break
    except Exception as e:
        await ws.send_json({"type": "error", "message": str(e)})
    finally:
        if session:
            await session.close()
```

#### Static file serving

- Mount `web/static/` → serves `index.html`
- Mount `results_dir` at `/screenshots/results/` → agent run screenshots
- Mount `demos_dir` at `/screenshots/demos/` → human demo screenshots
- CORS not needed (same origin)

#### Startup

```python
import argparse, pathlib
from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
import uvicorn

app = FastAPI()

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8000)
parser.add_argument("--results-dir", type=str, default="./results")
parser.add_argument("--annotations-dir", type=str, default="./annotations")
parser.add_argument("--demos-dir", type=str, default="./human_demos")
args = parser.parse_args()

RESULTS_DIR = pathlib.Path(args.results_dir).resolve()
ANNOTATIONS_DIR = pathlib.Path(args.annotations_dir).resolve()
DEMOS_DIR = pathlib.Path(args.demos_dir).resolve()

app.mount("/static", StaticFiles(directory="web/static"), name="static")
app.mount("/screenshots/results", StaticFiles(directory=str(RESULTS_DIR)), name="results_screenshots")
app.mount("/screenshots/demos", StaticFiles(directory=str(DEMOS_DIR)), name="demos_screenshots")

# ... endpoints + websocket ...

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=args.port)
```

---

### 2. `web/static/index.html` — Single-page frontend

One HTML file, ~900–1300 lines, four tabs managed by JS.

**CSS:** Dark theme (`--bg: #0f1117`, `--surface: #1a1b26`, `--text: #c9d1d9`, `--accent: #7aa2f7`). Wide screenshot panel (500px+ for browser viewport captures).

#### Tab 1: Browse

- On load: `GET /api/runs` → render list grouped by benchmark, then model
- Each group collapsible; each run row shows: source badge (agent/demo), task_id, model/annotator, step count, score badge (PASS/FAIL/unknown), annotation status dot
- Click a row → slideshow opens inline (or in a panel)
- **Slideshow:**
  - Left: screenshot (scaled to fit, click to zoom)
  - Right: action JSON (syntax-highlighted), reasoning text, observation summary
  - Navigation: arrow keys, left/right buttons, progress bar
  - Step counter: "Step 3 / 12"
- Filter bar: text search (filters on task_id, model, benchmark) + source filter (All / Agent Runs / Human Demos) + status filter (all / annotated / unannotated)
- Refresh button → `GET /api/runs/refresh`

#### Tab 2: Annotate

- **Run selector:** dropdown or searchable list (from same `/api/runs` data), filtered to unannotated-first
- Selecting a run opens the same slideshow layout, but the right panel adds grading controls:
- **Per-step grading:**
  - Verdict: radio buttons — correct / incorrect / ambiguous
  - If incorrect: correction form — action type dropdown (click, type, scroll, goto, key_press, select, stop) + relevant fields (x/y coords, text, selector)
  - Optional note field
- **Final assessment** (shown after last step or as a separate sub-panel):
  - Success: Yes / No toggle
  - Failure mode: dropdown — `["correct_completion", "early_stop", "wrong_action_sequence", "navigation_failure", "interaction_failure", "timeout", "other"]` (configurable)
  - Reasoning: textarea
  - Annotator name: text input (remembered in localStorage)
- **Submit** → `POST /api/annotations` with all step verdicts + task verdict
- **Pre-fill:** if annotations exist (`GET /api/annotations/{run_id}`), load them into the form
- **Timer:** starts when run is opened, duration saved with annotation

#### Tab 3: Record Demo

- **Top bar:** task ID input (free text or dropdown if tasks known), start URL input, annotator name input (remembered in localStorage), "Start Recording" button
- **When recording:**
  - Large screenshot area (~1280×720 scaled to fit), updated in real-time via WebSocket
  - Click on screenshot → translates pixel position to viewport coordinates (accounting for CSS scaling), shows coordinate tooltip with crosshair
  - **Action palette** below screenshot:
    - **Click:** click on screenshot to set (x, y), confirm button sends action
    - **Type:** text input field + submit button
    - **Scroll:** direction buttons (up/down/left/right) + pixel amount input (default 300)
    - **Navigate:** URL input + go button
    - **Key Press:** key selector (Enter, Tab, Escape, Backspace, ArrowUp/Down/Left/Right, or custom)
    - **Select:** CSS selector input + value input
    - **Stop:** ends recording, shows success/fail dialog with optional answer field
  - **Step history sidebar** (right side or bottom): thumbnails of each recorded step with step number + action label (e.g., "click (450, 320)", "type 'hello'"), click to review
- **Coordinate handling:**
  - Screenshot is displayed at CSS size; actual viewport is 1280×720
  - On click: `realX = (clickX / displayedWidth) * 1280`, same for Y
  - Show crosshair overlay at click position + tooltip with "(x, y)" before confirming
- **WebSocket messages:**
  - Client → Server: `{"type": "start", "url": "...", "task_id": "...", "annotator": "..."}`
  - Client → Server: `{"type": "action", "action": {"type": "click", "x": 450, "y": 320}}` (or type/scroll/goto/key_press/select)
  - Client → Server: `{"type": "stop", "success": true, "answer": "..."}`
  - Server → Client: `{"type": "recording_started", "step": 0, "image_b64": "...", "url": "..."}`
  - Server → Client: `{"type": "screenshot", "step": N, "image_b64": "...", "url": "...", "title": "..."}`
  - Server → Client: `{"type": "recording_stopped", "task_dir": "..."}`
  - Server → Client: `{"type": "error", "message": "..."}`
- **On stop:** show confirmation toast with link to view the new demo in Browse tab. Auto-trigger a runs refresh.

#### Tab 4: Dashboard

- Fetches `GET /api/dashboard`
- **Progress bar:** annotated / total runs
- **Per-model table:** model name, total runs, annotated count, pass rate, avg score
- **Failure mode breakdown:** horizontal bar chart (pure CSS — `div` bars with percentage widths)
- **Recent annotations:** table of last 20 annotations with timestamp, annotator, run_id, verdict
- **Demo stats:** count of recorded demos, demos per annotator

---

### 3. `web/recorder.py` — Playwright demo recorder

Manages Playwright browser sessions, controlled via WebSocket from `app.py`.

```python
class DemoSession:
    """
    One recording session = one Playwright browser instance.
    Produces output in the same format as agent runs so it appears in Browse.
    """

    async def start(self, url: str, task_id: str, annotator: str,
                    demos_dir: Path, viewport: tuple = (1280, 720)) -> str:
        """
        Launch headless Chromium, navigate to url, take initial screenshot.
        Creates output dir: demos_dir/{annotator}/{task_id}/
        Returns initial screenshot as base64 string.
        """
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context(viewport={
            "width": viewport[0], "height": viewport[1]
        })
        self.page = await self.context.new_page()
        await self.page.goto(url, wait_until="networkidle")

        # Create output directory
        self.task_dir = demos_dir / annotator / task_id
        self.steps_dir = self.task_dir / "steps"
        self.steps_dir.mkdir(parents=True, exist_ok=True)

        # Internal state
        self.step_count = 0
        self.trajectory = []
        self.task_id = task_id
        self.annotator = annotator
        self.start_url = url

        # Initial screenshot (step 0)
        screenshot_b64 = await self._take_screenshot()
        self._save_step(screenshot_b64, action=None, observation={
            "url": self.page.url,
            "title": await self.page.title(),
        })
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
            await self.page.goto(action["url"], wait_until="networkidle")
        elif action_type == "key_press":
            await self.page.keyboard.press(action["key"])
        elif action_type == "hover":
            await self.page.mouse.move(action["x"], action["y"])
        elif action_type == "select":
            await self.page.select_option(action["selector"], action["value"])

        # Wait for page to settle
        await self.page.wait_for_timeout(500)

        self.step_count += 1
        screenshot_b64 = await self._take_screenshot()
        self._save_step(screenshot_b64, action=action, observation={
            "url": self.page.url,
            "title": await self.page.title(),
        })
        return screenshot_b64

    async def stop(self, success: bool, answer: str = None) -> Path:
        """
        Finalize recording. Write trajectory.json, task.json, run_metadata.json.
        Close browser. Returns output directory path.
        """
        (self.task_dir / "trajectory.json").write_text(
            json.dumps(self.trajectory, indent=2)
        )
        (self.task_dir / "task.json").write_text(json.dumps({
            "task_id": self.task_id,
            "url": self.start_url,
        }, indent=2))
        (self.task_dir / "run_metadata.json").write_text(json.dumps({
            "source": "human_demo",
            "annotator": self.annotator,
            "task_id": self.task_id,
            "success": success,
            "answer": answer,
            "step_count": self.step_count,
            "timestamp": datetime.now().isoformat(),
        }, indent=2))
        await self.close()
        return self.task_dir

    async def close(self):
        """Cleanup browser resources."""
        if hasattr(self, "browser") and self.browser:
            await self.browser.close()
        if hasattr(self, "playwright") and self.playwright:
            await self.playwright.stop()

    async def _take_screenshot(self) -> str:
        png_bytes = await self.page.screenshot(type="png")
        return base64.b64encode(png_bytes).decode()

    def _save_step(self, screenshot_b64: str, action: dict | None,
                   observation: dict):
        step_dir = self.steps_dir / f"{self.step_count:02d}"
        step_dir.mkdir(exist_ok=True)
        (step_dir / "screenshot.png").write_bytes(
            base64.b64decode(screenshot_b64)
        )
        if action is not None:
            (step_dir / "action.json").write_text(json.dumps(action, indent=2))
        (step_dir / "observation.json").write_text(
            json.dumps(observation, indent=2)
        )
        self.trajectory.append({
            "step": self.step_count,
            "action": action,
            "observation": observation,
        })

    @property
    def current_url(self) -> str:
        return self.page.url

    @property
    def page_title(self) -> str:
        return self.trajectory[-1]["observation"].get("title", "") if self.trajectory else ""
```

---

### 4. `web/requirements.txt`

```
fastapi>=0.104.0
uvicorn>=0.24.0
websockets>=12.0
playwright>=1.40.0
```

After install: `playwright install chromium` to pull the browser binary.

---

## What Changed vs. the SSH-Only Plan

| SSH-only plan | Updated (local) | Reason |
|---|---|---|
| 3 tabs (Browse, Annotate, Dashboard) | 4 tabs (+Record Demo) | Local machine has a browser Playwright can drive |
| No `recorder.py` | `web/recorder.py` with `DemoSession` class | Playwright controls headless Chromium, streams screenshots |
| No WebSocket | `WS /ws/record` endpoint | Real-time screenshot streaming during recording |
| Only `results_dir` scanned | `results_dir` + `demos_dir` both scanned | Recorded demos saved separately, both appear in Browse |
| Only `fastapi` + `uvicorn` | + `websockets` + `playwright` | Recording needs these |
| No `--demos-dir` flag | `--demos-dir ./human_demos` | Separate output directory for recorded demos |
| Single `/screenshots/` mount | `/screenshots/results/` + `/screenshots/demos/` | Two source directories need separate mounts |

Everything else (self-contained discovery, SQLite annotations, dark-theme frontend) is unchanged.

---

## Discovery: Data-First, Not Schema-First

The discovery module has **no hardcoded assumptions** about directory structure or file naming. Instead:

1. The implementing agent **inspects the disk first** (tree + sample files) during implementation.
2. Discovery logic is written to match what's actually there.
3. If the data layout changes later, the discovery code is updated to match — not the other way around.

The only universal rule: a "run" is a directory that contains a sequence of screenshots (possibly with associated action/observation data). Everything else — nesting depth, file names, metadata format, step numbering scheme — is determined by reading the data.

---

## Demo Output Format

Recorded demos are saved in a structure that the discovery module can find. The recorder writes a known, minimal layout:

```
human_demos/
  {annotator}/
    {task_id}/
      run_metadata.json    # {"source": "human_demo", "annotator": "...", "success": true, ...}
      task.json            # {"task_id": "...", "url": "..."}
      trajectory.json      # [{"step": 0, "action": null, "observation": {...}}, ...]
      steps/
        00/screenshot.png, observation.json
        01/screenshot.png, action.json, observation.json
        ...
```

Since we control this format (unlike the downloaded agent data), the discovery module should handle it explicitly. The recorder's format is the **one layout that IS hardcoded** — it's our own output, not external data.

---

## Implementation Order

1. **Inspect the data** — Tree the results directory, sample JSON files, understand the actual layout. This happens before writing any code.
2. **`web/app.py`** — Discovery logic (tailored to what was found in step 1) + `/api/runs` + `/api/trajectory` + screenshot serving. Verify it finds and loads the real data.
3. **`web/static/index.html`** — Browse tab with list view + slideshow. Get the core viewing loop working with local data.
4. **Annotate tab** — Add grading controls, wire up `POST /api/annotations`, SQLite storage.
5. **`web/recorder.py` + Record Demo tab** — Playwright session + WebSocket + frontend with coordinate-mapped click handling.
6. **Dashboard tab** — Analytics from annotation data + demo stats.
7. **Export endpoint** — Dump annotations as JSONL.

---

## Verification

1. **Browse:** `python web/app.py --results-dir ./results` → open `localhost:8000` → see downloaded runs listed → click one → slideshow with screenshots loads
2. **Annotate:** select a run → grade steps → submit → verify `annotations/annotations.db` has data
3. **Record Demo:** select task → enter URL → Start Recording → see live screenshot → click/type actions → screenshots update → Stop → verify `human_demos/` output → new demo appears in Browse tab after refresh
4. **Dashboard:** see annotation progress, per-model breakdown, demo counts
