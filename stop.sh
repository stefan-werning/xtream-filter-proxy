#!/bin/bash
# Stops the Xtream Filter Proxy started by start.sh.
#
# IMPORTANT: this sends SIGTERM, not SIGKILL. On SIGTERM, main.py's lifespan
# shutdown waits for the crawler thread to finish its current probe (up to
# 120s) before the process actually exits -- so an in-flight ffprobe call or
# upstream request always completes cleanly instead of being cut off, which
# matters a lot on accounts with only 1 allowed connection. Never use
# `kill -9` / SIGKILL directly against this process for that reason; if you
# must force it, you risk leaving the account's only connection slot stuck
# open until the provider times it out on its own.
cd "$(dirname "$0")"

PID_FILE="./data/uvicorn.pid"
# Must be a bit longer than --timeout-graceful-shutdown in start.sh, which
# itself must be >= the timeout passed to CrawlerWorker.stop() in main.py.
GRACE_SECONDS=140

if [ ! -f "$PID_FILE" ]; then
  echo "No PID file found ($PID_FILE) — not running via start.sh?"
  exit 0
fi

PID="$(cat "$PID_FILE")"

if kill -0 "$PID" 2>/dev/null; then
  echo "Sending SIGTERM (PID $PID) -- waiting up to ${GRACE_SECONDS}s for an in-flight probe to finish cleanly..."
  kill "$PID"
  waited=0
  while kill -0 "$PID" 2>/dev/null; do
    if [ "$waited" -ge "$GRACE_SECONDS" ]; then
      echo "Process $PID did not stop within ${GRACE_SECONDS}s."
      echo "Refusing to SIGKILL automatically -- that could abandon the account's only open connection."
      echo "Check ./data/uvicorn.log for what the crawler is doing, then re-run this script to keep waiting,"
      echo "or kill -9 $PID manually if you're certain no probe is in flight."
      exit 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
  echo "Stopped cleanly (PID $PID) after ${waited}s."
else
  echo "Process $PID not running."
fi

rm -f "$PID_FILE"
