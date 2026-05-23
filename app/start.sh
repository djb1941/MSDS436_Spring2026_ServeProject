#!/bin/bash
# start.sh — launch both processes inside the python-app container.
#
# We use a simple background + wait pattern:
#   - The Flet web UI runs in the background.
#   - The simulation engine runs in the foreground (so Docker sees its exit code).
# If either process dies, `wait -n` catches it and the script exits,
# which causes Docker to restart the container per the restart policy.

set -e

echo "[start.sh] Starting Flet web UI on port 8501..."
python3 /app/frontend/app/main.py &
FLET_PID=$!

echo "[start.sh] Starting simulation engine..."
python3 /app/main.py &
SIM_PID=$!

echo "[start.sh] Both processes running. Flet PID=$FLET_PID, Sim PID=$SIM_PID"

# Exit if either child exits (requires bash 4.3+, which Fedora 40 ships)
wait -n $FLET_PID $SIM_PID
echo "[start.sh] A child process exited — shutting down container."
