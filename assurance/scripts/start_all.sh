#!/usr/bin/env bash
# Launch all 6 RAI Trust Platform services in labeled Terminal.app windows,
# in the order required by Sharad's start_all.bat (Guardrail -> PE -> PAP),
# then pause for manual app onboarding via PAP UI, then launch the apps.
#
# macOS equivalent of start_all.bat — uses osascript to drive Terminal.app.
#
# Usage:
#   bash scripts/start_all.sh
#
# Prereq:
#   - All 6 service ports must be FREE (script bails if any are listening)
#   - bootstrap.py must have been run (DB exists with seeded apps)
#
# After this finishes, run scripts/path_b_hydrate_test.sh to seed traffic
# and verify metrics.

set -euo pipefail

# ── Paths ────────────────────────────────────────────────────────────────────
PROTOTYPE="/Users/981061/Documents/Claude/Projects/RAI May manifestation/rai-trust-platform-prototype"
ASSURANCE="/Users/981061/Documents/Claude/Projects/RAI May manifestation/rai-assurance"
LOANWISE="/Users/981061/Documents/Claude/Projects/RAI May manifestation/rai-clients/loanapp"

# ── Output helpers ───────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; RESET='\033[0m'
ok()   { echo -e "  ${GREEN}[OK]${RESET} $*"; }
warn() { echo -e "  ${YELLOW}[!!]${RESET} $*"; }
err()  { echo -e "  ${RED}[XX]${RESET} $*"; }
hdr()  { echo; echo -e "${BOLD}>>  $*${RESET}"; }

# ── Helper: open a new Terminal.app window with a title + command ────────────
open_terminal() {
  local title="$1"
  local cmd="$2"

  # Use osascript to spawn a new Terminal.app window, set its title, and run
  # the command inside it. The 'do script' AppleScript verb returns the new
  # tab so we can set its custom title for clear labeling during the demo.
  #
  # The cd paths contain spaces and double quotes, which clash with AppleScript's
  # own string delimiter. We escape every embedded " as \" so AppleScript reads
  # them as literal characters inside the do-script string.
  local escaped_cmd="${cmd//\"/\\\"}"

  if ! osascript <<EOF
tell application "Terminal"
    activate
    set newTab to do script "$escaped_cmd"
    set custom title of newTab to "$title"
end tell
EOF
  then
    err "Failed to open Terminal.app window for '$title' via osascript."
    err "Likely cause: AppleScript Automation permission not granted."
    err "Fix: System Settings → Privacy & Security → Automation → enable Terminal."
    err "Or run the service manually:"
    err "  $cmd"
    return 1
  fi
}

# ── Pre-flight: are tracked ports free? ──────────────────────────────────────
hdr "Pre-flight — confirm all tracked ports are free"

PORTS_TO_CHECK="3001 3002 3003 3010 5000 8002"
RUNNING=""
for p in $PORTS_TO_CHECK; do
  if lsof -nP -iTCP:$p -sTCP:LISTEN 2>/dev/null | grep -q LISTEN; then
    RUNNING+="$p "
  fi
done

if [[ -n "$RUNNING" ]]; then
  err "Ports already in use: $RUNNING"
  warn "Press Ctrl+C in any running service terminals, or run stop helper."
  warn "Skipping launch to avoid port conflicts."
  exit 1
fi
ok "All 6 tracked ports are free"

# ── Phase 1: core services (Guardrail -> PE -> PAP) ──────────────────────────
hdr "Phase 1 — Launching core services in order"

echo "  Launching Guardrail Service (3001)..."
open_terminal "RAI - Guardrail [3001]" \
  "cd \"$PROTOTYPE/guardrail-service\" && source ../venv/bin/activate && python main.py"
echo "  Waiting 4s for Guardrail to bind..."
sleep 4

echo "  Launching Policy Engine (3003)..."
open_terminal "RAI - Policy Engine [3003]" \
  "cd \"$PROTOTYPE/policy-engine\" && source ../venv/bin/activate && python main.py"
echo "  Waiting 3s for PE to register with Guardrail..."
sleep 3

echo "  Launching PAP Dashboard (3002)..."
open_terminal "RAI - PAP Dashboard [3002]" \
  "cd \"$PROTOTYPE/pap\" && source ../venv/bin/activate && python main.py"
echo "  Waiting 3s for PAP to come up..."
sleep 3

# ── Manual onboarding pause ──────────────────────────────────────────────────
hdr "Phase 2 — Manual: onboard loanapp via PAP UI"

cat <<'INSTRUCTIONS'

  Open in browser:  http://localhost:3002/ui

  In the App Stack tab:
    - Onboard loanapp:
        display_name = "LoanWise"
        packs        = sanctions + session_guard
        enforcement  = BLOCK

  (mybank + wealthai are pre-seeded — no action needed for them.)

  Press ENTER here once loanapp is onboarded, and I'll launch the rest.
INSTRUCTIONS

# Read with no variable — just blocks until Enter
read -r

# ── Phase 3: app servers ─────────────────────────────────────────────────────
hdr "Phase 3 — Launching Assurance API + client apps"

echo "  Launching Assurance API (3010) with LLM-cache enabled..."
open_terminal "RAI - Assurance API [3010]" \
  "cd \"$ASSURANCE\" && source .venv/bin/activate && EVAL_FAST_MODE=true METRICS_USE_LLM_CACHE=true LLM_EVAL_CACHE_ENABLED=true uvicorn api.main:app --port 3010 --reload"
sleep 2

echo "  Launching MyBank (5000)..."
open_terminal "RAI - MyBank [5000]" \
  "cd \"$PROTOTYPE/sdk/examples/mybank_clean\" && source venv/bin/activate && python server.py"
sleep 2

echo "  Launching LoanWise (8002)..."
open_terminal "RAI - LoanWise [8002]" \
  "cd \"$LOANWISE\" && source venv/bin/activate && python server.py"

# ── Done ─────────────────────────────────────────────────────────────────────
hdr "All 6 services launched"

cat <<'DONE'

  Anchor URLs:
    PAP Dashboard      : http://localhost:3002/ui/dashboard
    Assurance Swagger  : http://localhost:3010/v1/docs
    Assurance metrics  : http://localhost:3010/v1/metrics?app_id=loanapp&window=1h
    PE health          : http://localhost:3003/v1/health
    Guardrail health   : http://localhost:3001/v1/health
    MyBank UI          : http://localhost:5000
    LoanWise UI        : http://localhost:8002

  Next:
    bash scripts/path_b_hydrate_test.sh
        — seeds traffic + smoke-tests /v1/metrics on the new ports

  To stop everything:
    bash scripts/stop_all.sh
DONE
