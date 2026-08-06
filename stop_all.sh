#!/usr/bin/env bash
# RAI Trust Platform — Stop All Services (macOS/Linux)
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGDIR="$ROOT/.launch-logs/run"

# Kill tracked PIDs first
if [ -f "$LOGDIR/pids" ]; then
  while read -r pid; do [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; done <"$LOGDIR/pids"
fi

# Sweep any survivors holding the service ports
for p in 3001 3002 3003 3010 3012 3013 5000; do
  pids=$(lsof -ti tcp:"$p" 2>/dev/null || true)
  [ -n "$pids" ] && { echo "[stop] killing $pids on :$p"; kill -9 $pids 2>/dev/null || true; }
done
: >"$LOGDIR/pids" 2>/dev/null || true
echo "[stop] all services stopped."
