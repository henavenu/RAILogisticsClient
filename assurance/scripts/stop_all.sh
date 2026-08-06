#!/usr/bin/env bash
# Stop all 6 RAI Trust Platform services by killing the processes listening
# on tracked ports. Cleanly closes each (TERM first, then KILL after grace).
#
# Usage:
#   bash scripts/stop_all.sh
#
# Safe to re-run — silently skips ports that are already free.

set -uo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; RESET='\033[0m'
ok()   { echo -e "  ${GREEN}[OK]${RESET} $*"; }
warn() { echo -e "  ${YELLOW}[!!]${RESET} $*"; }
hdr()  { echo; echo -e "${BOLD}>>  $*${RESET}"; }

# Tracked services — parallel arrays of "port:friendly name" pairs.
# Avoids bash 4+ associative arrays so this runs on macOS's default bash 3.2.
SERVICES=(
  "3001:Guardrail"
  "3002:PAP Dashboard"
  "3003:Policy Engine"
  "3010:Assurance API"
  "5000:MyBank"
  "8002:LoanWise"
)

kill_port() {
  # Kills ALL processes listening on a port (parent + uvicorn --reload workers
  # frequently linger after only the parent gets TERM). Uses TERM first for a
  # graceful close, then KILL after a short grace window.
  local port="$1"
  local name="$2"

  local pids
  pids=$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | tr '\n' ' ')
  if [[ -z "$pids" ]]; then
    warn "$name ($port) — already stopped"
    return 0
  fi

  # TERM all listening pids
  for pid in $pids; do
    kill -TERM "$pid" 2>/dev/null || true
  done

  # Wait up to 3 seconds for them to exit
  for _ in 1 2 3; do
    local alive=""
    for pid in $pids; do
      if kill -0 "$pid" 2>/dev/null; then
        alive+="$pid "
      fi
    done
    [[ -z "$alive" ]] && break
    sleep 1
  done

  # Anything still listening on this port? Force-kill (handles uvicorn reload
  # workers that survived TERM, plus any new children spawned in the meantime).
  local remaining
  remaining=$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | tr '\n' ' ')
  if [[ -n "$remaining" ]]; then
    for pid in $remaining; do
      kill -KILL "$pid" 2>/dev/null || true
    done
    ok "$name ($port) — force-killed (pids: $remaining)"
  else
    ok "$name ($port) — stopped cleanly (pids: $pids)"
  fi
}

hdr "Stopping all 6 services"

for spec in "${SERVICES[@]}"; do
  port="${spec%%:*}"
  name="${spec#*:}"
  kill_port "$port" "$name"
done

hdr "Verifying all ports are free"

STILL_LISTENING=""
for spec in "${SERVICES[@]}"; do
  port="${spec%%:*}"
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | grep -q LISTEN; then
    STILL_LISTENING+="$port "
  fi
done

if [[ -n "$STILL_LISTENING" ]]; then
  warn "Ports still listening: $STILL_LISTENING"
  warn "Inspect with: lsof -nP -iTCP:<port> -sTCP:LISTEN"
else
  ok "All 6 ports free"
fi
