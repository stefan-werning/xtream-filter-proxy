#!/bin/bash
# Cleanly restarts the Xtream Filter Proxy: stop.sh (graceful, waits for any
# in-flight probe) followed by start.sh. Use this instead of manually
# killing and restarting, so the crawler never gets cut off mid-probe.
set -e
cd "$(dirname "$0")"

./stop.sh
./start.sh
