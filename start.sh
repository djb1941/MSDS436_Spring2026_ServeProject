#!/bin/bash
# start.sh — launch all processes inside the python-app container.
#
# Order of operations:
#   1. setup.py   — download OSM data + populate PostGIS (skips if already done)
#   2. Flet UI    — web frontend on port 8501 (background)
#   3. Sim engine — SimPy simulation engine (background)
#
# If either long-running process exits, `wait -n` catches it and the script
# exits, causing Docker to restart the container per its restart policy.

set -e

echo "[start.sh] Running data setup (idempotent — fast if already done)..."
python3 /app/setup.py

echo "[start.sh] Starting Flet web UI on port 8501..."
flet run --web --host 0.0.0.0 --port 8501 /app/frontend/app/main.py &
FLET_PID=$!

echo "[start.sh] Starting simulation engine..."
python3 /app/main.py &
SIM_PID=$!

echo "[start.sh] All processes running. Flet PID=$FLET_PID, Sim PID=$SIM_PID"

# Exit if either child exits (requires bash 4.3+, which Fedora 40 ships)
wait -n $FLET_PID $SIM_PID
echo "[start.sh] A child process exited — shutting down container."
