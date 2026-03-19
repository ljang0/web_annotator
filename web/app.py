"""
FastAPI server for trajectory browsing, annotation, demo recording, and analytics.
Usage: python web/app.py [--port 8000] [--results-dir ./runs_journeys_clustered_chains_v2_100steps]
                         [--annotations-dir ./annotations] [--demos-dir ./human_demos]
                         [--tasks-file ./datasets/clustered_chains_v2_tasks_with_rubrics.json]
"""

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# ---------------------------------------------------------------------------
# CLI args — use parse_known_args to avoid conflicts with uvicorn CLI
# ---------------------------------------------------------------------------
import os

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8000)
# Default results dir: look in parent directory (Desktop) or use env var
_default_results = os.environ.get("RESULTS_DIR", "")
if not _default_results:
    # Try common locations
    for candidate in [
        Path(__file__).resolve().parent.parent / "runs_journeys_clustered_chains_v2_100steps",
        Path.cwd() / "runs_journeys_clustered_chains_v2_100steps",
        Path.cwd().parent / "runs_journeys_clustered_chains_v2_100steps",
        Path.home() / "Desktop" / "runs_journeys_clustered_chains_v2_100steps",
    ]:
        if candidate.exists():
            _default_results = str(candidate)
            break
    if not _default_results:
        _default_results = "./results"

parser.add_argument("--results-dir", type=str, default=_default_results)
parser.add_argument("--annotations-dir", type=str,
                    default=os.environ.get("ANNOTATIONS_DIR", "./annotations"))
parser.add_argument("--demos-dir", type=str,
                    default=os.environ.get("DEMOS_DIR", "./human_demos"))
parser.add_argument("--tasks-file", type=str,
                    default=os.environ.get("TASKS_FILE", "./datasets/clustered_chains_v2_tasks_with_rubrics.json"))
args, _unknown = parser.parse_known_args()

RESULTS_DIR = Path(args.results_dir).resolve()
ANNOTATIONS_DIR = Path(args.annotations_dir).resolve()
DEMOS_DIR = Path(args.demos_dir).resolve()
TASKS_FILE = Path(args.tasks_file).resolve()

ANNOTATIONS_DIR.mkdir(parents=True, exist_ok=True)
DEMOS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI()

# ---------------------------------------------------------------------------
# Task descriptions / rubrics (loaded once)
# ---------------------------------------------------------------------------
TASKS_BY_ID: dict[str, dict] = {}
if TASKS_FILE.exists():
    with open(TASKS_FILE) as f:
        for t in json.load(f):
            TASKS_BY_ID[t["task_id"]] = t

# ---------------------------------------------------------------------------
# Eval results (loaded once for score lookup)
# ---------------------------------------------------------------------------
EVAL_RESULTS_BY_TASK: dict[str, dict] = {}
eval_results_file = RESULTS_DIR / "eval_results.json"
if eval_results_file.exists():
    with open(eval_results_file) as f:
        eval_data = json.load(f)
        # Can be a dict with "tasks" key or a list
        if isinstance(eval_data, dict):
            entries = eval_data.get("tasks", [])
        else:
            entries = eval_data
        for entry in entries:
            if isinstance(entry, dict) and "task_id" in entry:
                EVAL_RESULTS_BY_TASK[entry["task_id"]] = entry

SUMMARY_BY_TASK: dict[str, dict] = {}
summary_file = RESULTS_DIR / "summary" / "results.json"
if summary_file.exists():
    with open(summary_file) as f:
        summary_data = json.load(f)
        # Can be a list of task entries or a dict with "tasks" key
        if isinstance(summary_data, dict):
            task_list = summary_data.get("tasks", [])
        else:
            task_list = summary_data
        for task_entry in task_list:
            if isinstance(task_entry, dict) and "task_id" in task_entry:
                SUMMARY_BY_TASK[task_entry["task_id"]] = task_entry

# ---------------------------------------------------------------------------
# JSONL annotation storage
# Layout: annotations/{annotator}/{task_id}.jsonl
# Each line is a complete annotation record (step verdicts, rubric, assessment)
# ---------------------------------------------------------------------------

def _annotation_path(annotator: str, task_id: str) -> Path:
    """Get the JSONL file path for an annotator + task."""
    return ANNOTATIONS_DIR / annotator / f"{task_id}.jsonl"


def _load_annotation(annotator: str, task_id: str) -> dict | None:
    """Load the latest annotation for an annotator + task."""
    p = _annotation_path(annotator, task_id)
    if not p.exists():
        return None
    # Return the last line (most recent)
    last = None
    for line in p.read_text().strip().splitlines():
        if line.strip():
            try:
                last = json.loads(line)
            except json.JSONDecodeError:
                pass
    return last


def _save_annotation(data: dict):
    """Append an annotation record as a JSONL line."""
    annotator = data.get("annotator", "anonymous")
    task_id = data.get("task_id", "")
    p = _annotation_path(annotator, task_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(data) + "\n")


def _get_all_annotated_run_ids() -> set:
    """Scan annotations dir to find all annotated run_ids."""
    annotated = set()
    if not ANNOTATIONS_DIR.exists():
        return annotated
    for jsonl_file in ANNOTATIONS_DIR.rglob("*.jsonl"):
        for line in jsonl_file.read_text().strip().splitlines():
            if line.strip():
                try:
                    rec = json.loads(line)
                    if "run_id" in rec:
                        annotated.add(rec["run_id"])
                except json.JSONDecodeError:
                    pass
    return annotated

# ---------------------------------------------------------------------------
# Discovery — adapted to actual data layout
#
# Layout found on disk:
#   {results_dir}/claude_computer_use/a11y_tree/{model}/mind2web_chrome/{task_hash}/
#     traj.jsonl   — one JSON object per line (step_num, action, response, screenshot_file, …)
#     result.txt   — float score
#     step_{N}_{timestamp}.png — screenshots
#
# Human demos layout (same format as agent runs):
#   {demos_dir}/{annotator}/{task_id}/
#     traj.jsonl, result.txt, run_metadata.json, step_N_timestamp.png
# ---------------------------------------------------------------------------

_runs_cache: list[dict] | None = None
_cache_time: float = 0
CACHE_TTL = 300  # seconds


def discover_agent_runs() -> list[dict]:
    """Walk results_dir looking for task directories containing traj.jsonl."""
    runs = []
    # Expected: results_dir / {action_space} / {obs_type} / {model} / {benchmark} / {task_id} /
    for traj_file in RESULTS_DIR.rglob("traj.jsonl"):
        task_dir = traj_file.parent
        task_id = task_dir.name

        # Extract path segments for metadata
        try:
            rel = task_dir.relative_to(RESULTS_DIR)
            parts = rel.parts  # e.g. ('claude_computer_use', 'a11y_tree', 'claude-sonnet-4-6', 'mind2web_chrome', '<hash>')
        except ValueError:
            parts = ()

        action_space = parts[0] if len(parts) > 0 else "unknown"
        obs_type = parts[1] if len(parts) > 1 else "unknown"
        model = parts[2] if len(parts) > 2 else "unknown"
        benchmark = parts[3] if len(parts) > 3 else "unknown"

        # Read score from result.txt
        result_file = task_dir / "result.txt"
        score = None
        if result_file.exists():
            try:
                score = float(result_file.read_text().strip())
            except (ValueError, OSError):
                pass

        # Count screenshots
        screenshots = sorted(task_dir.glob("step_*.png"))
        step_count = len(screenshots)

        # Get eval result info (eval_results.json has "success" bool, summary has "status" str)
        eval_info = EVAL_RESULTS_BY_TASK.get(task_id, {})
        if "success" in eval_info:
            status = "success" if eval_info["success"] else "failure"
        else:
            status = eval_info.get("status", "unknown")

        # Get task description
        task_info = TASKS_BY_ID.get(task_id, {})
        intent = task_info.get("confirmed_task", "")
        website = task_info.get("website", "")
        level = task_info.get("level", "")

        run_id = str(task_dir.relative_to(RESULTS_DIR))

        runs.append({
            "run_id": run_id,
            "source": "agent",
            "task_id": task_id,
            "model": model,
            "benchmark": benchmark,
            "action_space": action_space,
            "observation_type": obs_type,
            "step_count": step_count,
            "score": score,
            "status": status,
            "intent": intent,
            "website": website,
            "level": level,
            "annotation_status": "unknown",  # filled later
        })

    return runs


def discover_demo_runs() -> list[dict]:
    """Walk demos_dir looking for recorded demos (new traj.jsonl or legacy format)."""
    runs = []
    seen_dirs: set[str] = set()

    # Find all demo dirs: look for traj.jsonl (new) or run_metadata.json (legacy)
    demo_dirs: list[Path] = []
    for traj_file in DEMOS_DIR.rglob("traj.jsonl"):
        demo_dirs.append(traj_file.parent)
    for meta_file in DEMOS_DIR.rglob("run_metadata.json"):
        demo_dirs.append(meta_file.parent)

    for task_dir in demo_dirs:
        dir_key = str(task_dir)
        if dir_key in seen_dirs:
            continue
        seen_dirs.add(dir_key)

        task_id = task_dir.name
        run_id = str(task_dir.relative_to(DEMOS_DIR))

        # Read metadata if available
        meta_file = task_dir / "run_metadata.json"
        meta = {}
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        if not task_id:
            task_id = meta.get("task_id", task_dir.name)

        # Count steps: new format has step_*.png, legacy has steps/ subdirs
        has_traj_jsonl = (task_dir / "traj.jsonl").exists()
        if has_traj_jsonl:
            screenshots = sorted(task_dir.glob("step_*.png"))
            step_count = len(screenshots)
        else:
            steps_dir = task_dir / "steps"
            step_count = len(list(steps_dir.iterdir())) if steps_dir.exists() else 0

        # Read score from result.txt
        result_file = task_dir / "result.txt"
        score = None
        if result_file.exists():
            try:
                score = float(result_file.read_text().strip())
            except (ValueError, OSError):
                pass

        # Get task description from tasks file
        task_info = TASKS_BY_ID.get(task_id, {})

        runs.append({
            "run_id": run_id,
            "source": "human_demo",
            "task_id": task_id,
            "model": meta.get("annotator", "human"),
            "benchmark": "human_demo",
            "action_space": "human",
            "observation_type": "screenshot",
            "step_count": step_count,
            "score": score if score is not None else (1.0 if meta.get("success") else 0.0),
            "status": "success" if (score is not None and score > 0) or meta.get("success") else "failure",
            "intent": task_info.get("confirmed_task", ""),
            "website": task_info.get("website", ""),
            "level": task_info.get("level", ""),
            "annotation_status": "unknown",
        })

    return runs


def get_all_runs(force_refresh=False) -> list[dict]:
    global _runs_cache, _cache_time
    if not force_refresh and _runs_cache is not None and (time.time() - _cache_time) < CACHE_TTL:
        return _runs_cache

    runs = discover_agent_runs() + discover_demo_runs()

    # Fill annotation status
    annotated_run_ids = _get_all_annotated_run_ids()

    for run in runs:
        run["annotation_status"] = "annotated" if run["run_id"] in annotated_run_ids else "unannotated"

    runs.sort(key=lambda r: (r["benchmark"], r["model"], r["task_id"]))
    _runs_cache = runs
    _cache_time = time.time()
    return runs


# ---------------------------------------------------------------------------
# Trajectory loading
# ---------------------------------------------------------------------------

def load_agent_trajectory(run_id: str) -> list[dict]:
    """Load trajectory from traj.jsonl for an agent run."""
    task_dir = RESULTS_DIR / run_id
    traj_file = task_dir / "traj.jsonl"
    if not traj_file.exists():
        return []

    steps = []
    with open(traj_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            screenshot_file = entry.get("screenshot_file", "")
            # Build screenshot URL path
            screenshot_path = f"/screenshots/results/{run_id}/{screenshot_file}" if screenshot_file else None

            # Parse action details from the nested action structure
            action_data = entry.get("action", {})
            action_input = action_data.get("input", {}) if isinstance(action_data, dict) else {}

            steps.append({
                "step": entry.get("step_num", 0),
                "screenshot_path": screenshot_path,
                "screenshot_file": screenshot_file,
                "action": action_data,
                "action_type": action_input.get("action", action_data.get("action_type", "")),
                "action_input": action_input,
                "response": entry.get("response", ""),
                "reasoning": _extract_thinking(action_data),
                "reward": entry.get("reward", 0),
                "done": entry.get("done", False),
            })

    return steps


def _extract_thinking(action_data: dict) -> str:
    """Extract thinking/reasoning from raw_response field."""
    if not isinstance(action_data, dict):
        return ""
    raw = action_data.get("raw_response", "")
    if "[THINKING]" in raw:
        # Extract text between [THINKING] and next bracket tag
        start = raw.index("[THINKING]") + len("[THINKING]")
        # Find next tag
        for tag in ["[TEXT]", "[TOOL_USE]"]:
            idx = raw.find(tag, start)
            if idx != -1:
                return raw[start:idx].strip()
        return raw[start:].strip()
    return ""


def load_demo_trajectory(run_id: str) -> list[dict]:
    """Load trajectory from demo dir (new traj.jsonl or legacy trajectory.json)."""
    task_dir = DEMOS_DIR / run_id

    # New format: traj.jsonl (same as agent runs)
    traj_file = task_dir / "traj.jsonl"
    if traj_file.exists():
        steps = []
        with open(traj_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                screenshot_file = entry.get("screenshot_file", "")
                screenshot_path = f"/screenshots/demos/{run_id}/{screenshot_file}" if screenshot_file else None

                action_data = entry.get("action", {})
                action_input = action_data.get("input", {}) if isinstance(action_data, dict) else {}

                steps.append({
                    "step": entry.get("step_num", 0),
                    "screenshot_path": screenshot_path,
                    "screenshot_file": screenshot_file,
                    "action": action_data,
                    "action_type": action_input.get("action", action_data.get("action_type", "")),
                    "action_input": action_input,
                    "response": entry.get("response", ""),
                    "reasoning": "",
                    "reward": entry.get("reward", 0),
                    "done": entry.get("done", False),
                })
        return steps

    # Legacy format: trajectory.json + steps/NN/screenshot.png
    legacy_file = task_dir / "trajectory.json"
    if legacy_file.exists():
        try:
            traj = json.loads(legacy_file.read_text())
        except (json.JSONDecodeError, OSError):
            return []

        steps = []
        for entry in traj:
            step_num = entry.get("step", 0)
            screenshot_path = f"/screenshots/demos/{run_id}/steps/{step_num:02d}/screenshot.png"
            action = entry.get("action")
            steps.append({
                "step": step_num,
                "screenshot_path": screenshot_path,
                "action": action,
                "action_type": action.get("type", "") if action else "",
                "action_input": action or {},
                "response": "",
                "reasoning": "",
                "reward": 0,
                "done": False,
                "observation": entry.get("observation", {}),
            })
        return steps

    return []


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/tasks")
async def api_tasks():
    """Return all tasks from the tasks file for the Record Demo task picker."""
    tasks = []
    for tid, t in TASKS_BY_ID.items():
        tasks.append({
            "task_id": tid,
            "confirmed_task": t.get("confirmed_task", ""),
            "website": t.get("website", ""),
            "level": t.get("level", ""),
        })
    tasks.sort(key=lambda t: t["task_id"])
    return {"tasks": tasks, "total": len(tasks)}


@app.get("/api/models")
async def api_models():
    """Return distinct models found in agent runs."""
    runs = get_all_runs()
    models = sorted(set(r["model"] for r in runs if r["source"] == "agent"))
    return {"models": models}


@app.get("/api/runs")
async def api_runs():
    runs = get_all_runs()
    # Group by benchmark then model
    grouped: dict[str, dict[str, list]] = {}
    for r in runs:
        bm = r["benchmark"]
        mdl = r["model"]
        grouped.setdefault(bm, {}).setdefault(mdl, []).append(r)
    return {"runs": runs, "grouped": grouped, "total": len(runs)}


@app.get("/api/runs/refresh")
async def api_runs_refresh():
    runs = get_all_runs(force_refresh=True)
    return {"runs": runs, "total": len(runs), "refreshed": True}


@app.get("/api/trajectory/{run_id:path}")
async def api_trajectory(run_id: str, source: str = Query(default="agent")):
    if source == "human_demo":
        steps = load_demo_trajectory(run_id)
    else:
        steps = load_agent_trajectory(run_id)

    # Get task info
    # Extract task_id from run_id (last path segment for agent runs)
    task_id = run_id.split("/")[-1] if "/" in run_id else run_id
    task_info = TASKS_BY_ID.get(task_id, {})
    # Use eval_results (detailed) or summary as fallback
    eval_info = EVAL_RESULTS_BY_TASK.get(task_id, {})
    summary_info = SUMMARY_BY_TASK.get(task_id, {})
    detail_info = eval_info if eval_info else summary_info

    return {
        "run_id": run_id,
        "source": source,
        "steps": steps,
        "step_count": len(steps),
        "task_info": task_info,
        "summary_info": {
            "success": detail_info.get("success"),
            "blocked": detail_info.get("blocked"),
            "blocked_reasoning": detail_info.get("blocked_reasoning"),
            "final_reasoning": detail_info.get("final_reasoning"),
            "key_points": detail_info.get("key_points"),
        } if detail_info else {},
    }


@app.get("/api/annotations/{run_id:path}")
async def api_get_annotations(run_id: str):
    """Find all annotation records for this run_id across all annotators."""
    results = []
    if ANNOTATIONS_DIR.exists():
        for jsonl_file in ANNOTATIONS_DIR.rglob("*.jsonl"):
            for line in jsonl_file.read_text().strip().splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("run_id") == run_id:
                        results.append(rec)
                except json.JSONDecodeError:
                    pass

    # Return in the format the frontend expects
    # Use the latest record per annotator
    latest = {}
    for rec in results:
        latest[rec.get("annotator", "anonymous")] = rec

    if not latest:
        return {"run_id": run_id, "step_annotations": [], "task_annotations": []}

    # Take the most recent annotation (by any annotator)
    rec = list(latest.values())[-1]
    return {
        "run_id": run_id,
        "step_annotations": rec.get("step_annotations", []),
        "task_annotations": [{
            "success": rec.get("task_annotation", {}).get("success", False),
            "failure_mode": rec.get("task_annotation", {}).get("failure_mode", ""),
            "reasoning": rec.get("task_annotation", {}).get("reasoning", ""),
            "rubric_json": json.dumps(rec.get("task_annotation", {}).get("rubric", [])),
            "annotator": rec.get("annotator", ""),
        }] if rec.get("task_annotation") else [],
    }


@app.post("/api/annotations")
async def api_save_annotations(data: dict = None):
    if data is None:
        return JSONResponse({"error": "No data"}, status_code=400)

    # Add timestamp
    data["timestamp"] = datetime.now().isoformat()

    # Save as JSONL: annotations/{annotator}/{task_id}.jsonl
    _save_annotation(data)

    # Invalidate cache
    global _runs_cache
    _runs_cache = None

    return {"status": "saved", "run_id": data.get("run_id", "")}


@app.get("/api/dashboard")
async def api_dashboard():
    runs = get_all_runs()

    total = len(runs)
    agent_runs = [r for r in runs if r["source"] == "agent"]
    demo_runs = [r for r in runs if r["source"] == "human_demo"]
    annotated = sum(1 for r in runs if r["annotation_status"] == "annotated")

    # Per-model stats
    model_stats: dict[str, dict] = {}
    for r in agent_runs:
        m = r["model"]
        if m not in model_stats:
            model_stats[m] = {"total": 0, "annotated": 0, "pass": 0, "scores": []}
        model_stats[m]["total"] += 1
        if r["annotation_status"] == "annotated":
            model_stats[m]["annotated"] += 1
        if r.get("score") is not None and r["score"] > 0:
            model_stats[m]["pass"] += 1
        if r.get("score") is not None:
            model_stats[m]["scores"].append(r["score"])

    for m in model_stats:
        s = model_stats[m]
        s["pass_rate"] = s["pass"] / s["total"] if s["total"] > 0 else 0
        s["avg_score"] = sum(s["scores"]) / len(s["scores"]) if s["scores"] else 0

    # Scan all annotation JSONL files for failure modes and recent annotations
    failure_modes: dict[str, int] = {}
    all_annotations: list[dict] = []
    if ANNOTATIONS_DIR.exists():
        for jsonl_file in ANNOTATIONS_DIR.rglob("*.jsonl"):
            for line in jsonl_file.read_text().strip().splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    all_annotations.append(rec)
                    fm = rec.get("task_annotation", {}).get("failure_mode", "")
                    if fm:
                        failure_modes[fm] = failure_modes.get(fm, 0) + 1
                except json.JSONDecodeError:
                    pass

    # Recent annotations (sorted by timestamp desc)
    all_annotations.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    recent = [{
        "run_id": r.get("run_id", ""),
        "task_id": r.get("task_id", ""),
        "annotator": r.get("annotator", ""),
        "success": r.get("task_annotation", {}).get("success"),
        "failure_mode": r.get("task_annotation", {}).get("failure_mode", ""),
        "timestamp": r.get("timestamp", ""),
    } for r in all_annotations[:20]]

    # Demo stats
    demo_annotators: dict[str, int] = {}
    for r in demo_runs:
        ann = r["model"]  # annotator stored in model field for demos
        demo_annotators[ann] = demo_annotators.get(ann, 0) + 1

    return {
        "total_runs": total,
        "agent_runs": len(agent_runs),
        "demo_runs": len(demo_runs),
        "annotated": annotated,
        "model_stats": model_stats,
        "failure_modes": failure_modes,
        "recent_annotations": recent,
        "demo_annotators": demo_annotators,
    }


@app.get("/api/export")
async def api_export():
    """Export all annotations as JSON."""
    all_annotations = []
    if ANNOTATIONS_DIR.exists():
        for jsonl_file in ANNOTATIONS_DIR.rglob("*.jsonl"):
            for line in jsonl_file.read_text().strip().splitlines():
                if line.strip():
                    try:
                        all_annotations.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    all_annotations.sort(key=lambda r: r.get("timestamp", ""))
    return JSONResponse(
        content={"count": len(all_annotations), "data": all_annotations},
        headers={"Content-Disposition": "attachment; filename=annotations_export.json"},
    )


# ---------------------------------------------------------------------------
# WebSocket — demo recording
# ---------------------------------------------------------------------------

@app.websocket("/ws/record")
async def ws_record(ws: WebSocket):
    await ws.accept()
    session = None
    try:
        from web.recorder import DemoSession

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
                if session is None:
                    await ws.send_json({"type": "error", "message": "No active session"})
                    continue
                screenshot_b64 = await session.execute_action(msg["action"])
                await ws.send_json({
                    "type": "screenshot",
                    "step": session.step_count,
                    "image_b64": screenshot_b64,
                    "url": session.current_url,
                    "title": session.page_title,
                })
            elif msg["type"] == "stop":
                if session is None:
                    await ws.send_json({"type": "error", "message": "No active session"})
                    continue
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
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        if session:
            await session.close()


# ---------------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------------

# Custom screenshot endpoint to handle filenames with @ and other special chars
from starlette.responses import Response as StarletteResponse

@app.get("/screenshots/results/{file_path:path}")
async def serve_result_screenshot(file_path: str):
    """Serve files from results dir, handling special chars like @ in filenames."""
    from urllib.parse import unquote
    file_path = unquote(file_path)
    full_path = RESULTS_DIR / file_path
    if not full_path.exists() or not full_path.is_file():
        return JSONResponse({"error": "Not found"}, status_code=404)
    # Security: ensure path is within RESULTS_DIR
    try:
        full_path.resolve().relative_to(RESULTS_DIR.resolve())
    except ValueError:
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    suffix = full_path.suffix.lower()
    content_types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                     ".webp": "image/webp", ".gif": "image/gif", ".json": "application/json",
                     ".txt": "text/plain"}
    ct = content_types.get(suffix, "application/octet-stream")
    return StarletteResponse(content=full_path.read_bytes(), media_type=ct)

@app.get("/screenshots/demos/{file_path:path}")
async def serve_demo_screenshot(file_path: str):
    """Serve files from demos dir."""
    from urllib.parse import unquote
    file_path = unquote(file_path)
    full_path = DEMOS_DIR / file_path
    if not full_path.exists() or not full_path.is_file():
        return JSONResponse({"error": "Not found"}, status_code=404)
    try:
        full_path.resolve().relative_to(DEMOS_DIR.resolve())
    except ValueError:
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    suffix = full_path.suffix.lower()
    content_types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                     ".webp": "image/webp", ".gif": "image/gif", ".json": "application/json",
                     ".txt": "text/plain"}
    ct = content_types.get(suffix, "application/octet-stream")
    return StarletteResponse(content=full_path.read_bytes(), media_type=ct)

# Mount static files for frontend
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Results dir: {RESULTS_DIR}")
    print(f"Annotations dir: {ANNOTATIONS_DIR}")
    print(f"Demos dir: {DEMOS_DIR}")
    print(f"Tasks file: {TASKS_FILE}")
    print(f"Starting server on port {args.port}...")
    uvicorn.run(app, host="0.0.0.0", port=args.port)
