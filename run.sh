#!/usr/bin/env bash
# Launch the Trajectory Viewer web app
# Usage: ./run.sh [--port 8001] [--results-dir /path/to/runs]

set -e

cd "$(dirname "$0")"

# ---- Friendly error checks ----

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "❌ Python 3 is not installed."
  echo "   Install it from https://www.python.org/downloads/ or via your package manager."
  exit 1
fi

# Check Python version >= 3.10
PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [[ "$PY_MAJOR" -lt 3 ]] || { [[ "$PY_MAJOR" -eq 3 ]] && [[ "$PY_MINOR" -lt 10 ]]; }; then
  echo "❌ Python 3.10+ is required (found $PY_VERSION)."
  echo "   Upgrade from https://www.python.org/downloads/"
  exit 1
fi

# Check required packages
MISSING_PKGS=()
python3 -c "import fastapi" 2>/dev/null || MISSING_PKGS+=("fastapi")
python3 -c "import uvicorn" 2>/dev/null || MISSING_PKGS+=("uvicorn")
python3 -c "import websockets" 2>/dev/null || MISSING_PKGS+=("websockets")

if [[ ${#MISSING_PKGS[@]} -gt 0 ]]; then
  echo "❌ Missing Python packages: ${MISSING_PKGS[*]}"
  echo "   Run: pip install -r web/requirements.txt"
  exit 1
fi

# Check Playwright (optional — only needed for recording)
if ! python3 -c "import playwright" 2>/dev/null; then
  echo "⚠️  Playwright is not installed — demo recording will be unavailable."
  echo "   To enable recording: pip install playwright && playwright install chromium"
  echo ""
elif ! python3 -c "
from playwright.sync_api import sync_playwright
p = sync_playwright().start()
try:
    b = p.chromium.launch(headless=True, channel='chrome')
    b.close()
except Exception:
    try:
        b = p.chromium.launch(headless=True)
        b.close()
    except Exception:
        exit(1)
p.stop()
" 2>/dev/null; then
  echo "⚠️  Playwright browsers not installed — demo recording may not work."
  echo "   Run: playwright install chromium"
  echo ""
fi

# ---- Port selection ----

PORT="${PORT:-8001}"

# Parse --port from args
for arg in "$@"; do
  if [[ "$prev" == "--port" ]]; then
    PORT="$arg"
  fi
  prev="$arg"
done

# Find an open port starting from $PORT
while lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; do
  echo "Port $PORT is in use, trying $((PORT + 1))..."
  PORT=$((PORT + 1))
done

echo "Starting Trajectory Viewer on http://127.0.0.1:${PORT}"
python3 -c "
from web.app import app
import uvicorn
uvicorn.run(app, host='127.0.0.1', port=${PORT})
"
