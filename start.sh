#!/bin/bash
# Starts the Xtream Filter Proxy in the background.
set -e
cd "$(dirname "$0")"

PORT="${PORT:-8099}"
HOST="${HOST:-0.0.0.0}"
LOG_FILE="./data/uvicorn.log"
PID_FILE="./data/uvicorn.pid"

mkdir -p ./data

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Already running (PID $(cat "$PID_FILE"))."
  exit 0
fi

if [ ! -d ".venv" ]; then
  echo "No .venv found. Create one first: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

source .venv/bin/activate
# --timeout-graceful-shutdown gives the crawler enough time to finish an
# in-flight probe (ffprobe or an upstream API call) before the process
# exits -- must be >= the timeout passed to CrawlerWorker.stop() in main.py.
nohup uvicorn app.main:app --host "$HOST" --port "$PORT" --timeout-graceful-shutdown 130 > "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
disown

sleep 1
if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Started (PID $(cat "$PID_FILE")) on http://$HOST:$PORT — logs: $LOG_FILE"
else
  echo "Failed to start — check $LOG_FILE"
  exit 1
fi
