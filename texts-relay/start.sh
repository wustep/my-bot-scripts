#!/bin/bash
# Start the Texts puller after reboot. Secrets live beside this script (gitignored).
set -euo pipefail
RELAY_DIR="$(cd "$(dirname "$0")" && pwd)"
if ! pgrep -f "$RELAY_DIR/pull.py" >/dev/null; then
  nohup python3 "$RELAY_DIR/pull.py" >>"$RELAY_DIR/pull.log" 2>&1 &
  echo $! >"$RELAY_DIR/pull.pid"
fi
echo started
