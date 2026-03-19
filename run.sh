#!/usr/bin/env bash
# Launch the Trajectory Viewer web app
# Usage: ./run.sh [--port 8001] [--results-dir /path/to/runs]

set -e

cd "$(dirname "$0")"

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
