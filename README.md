# Web Agent Trajectory Annotator

A web app for browsing, annotating, and recording web agent trajectories. Built with FastAPI + vanilla JS.

## Features

- **Browse** — View agent run trajectories with pre-action screenshots, action details, reasoning, and judge evaluations
- **Annotate** — Per-step verdict (correct/incorrect/ambiguous), rubric grading with step tracking, final assessment
- **Record Demo** — Record human demonstrations via Playwright with a terminate action for final responses
- **Dashboard** — Annotation progress, per-model stats, failure mode distribution

## Setup

### Requirements

- Python 3.10+
- Google Chrome (for demo recording)

### Install

```bash
pip install -r web/requirements.txt
```

### Playwright (for demo recording only)

If you want to use the Record Demo feature, install Playwright's Chrome driver:

```bash
playwright install chromium
```

> **Note:** This downloads a Chromium binary (~150MB). The Browse, Annotate, and Dashboard tabs work without it.

## Usage

```bash
./run.sh
```

The server starts at `http://127.0.0.1:8001`. If the port is in use, it auto-increments to the next available port.

### Custom port

```bash
PORT=8080 ./run.sh
```

### Data directory

By default, the app looks for trajectory data at `runs_journeys_clustered_chains_v2_100steps` in the parent directory, current directory, or Desktop. Override with:

```bash
RESULTS_DIR=/path/to/runs ./run.sh
```

## Data Layout

### Agent runs (input)

```
{results_dir}/claude_computer_use/a11y_tree/{model}/mind2web_chrome/{task_hash}/
  traj.jsonl          # one JSON line per step
  result.txt          # float score (0.0 or 1.0)
  step_N_timestamp.png  # screenshots
```

### Human demos (output)

Recorded demos save in the same format under `human_demos/{annotator}/{task_id}/`.

### Annotations (output)

```
annotations/{annotator}/{task_id}.jsonl
```

Each line is a complete annotation record:

```json
{
  "run_id": "...",
  "task_id": "...",
  "annotator": "alice",
  "step_annotations": [
    {"step": 0, "verdict": "correct", "note": ""}
  ],
  "task_annotation": {
    "success": false,
    "failure_mode": "navigation_failure",
    "reasoning": "Agent went to wrong site",
    "rubric": [
      {"id": "R1", "requirement": "...", "grade": "pass", "step": 3},
      {"id": "R2", "requirement": "...", "grade": "fail", "step": 8}
    ]
  },
  "timestamp": "2026-03-18T20:35:35.206757"
}
```

## Task Rubrics

Task definitions and rubrics are loaded from `datasets/clustered_chains_v2_tasks_with_rubrics.json`. Each task includes structured rubric items with requirements and verification criteria that appear in the annotation interface.
