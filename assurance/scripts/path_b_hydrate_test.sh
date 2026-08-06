#!/usr/bin/env bash
# Path B — Phase 2: hydrate evaluation data and run smoke test.
#
# Run this AFTER path_b_rebuild.sh and after manually starting all 6 services
# and onboarding mybank + loanapp via PAP UI.
#
# What this does:
#   1. Verifies all 6 services are listening on the new ports
#   2. Runs run_scenarios.py for each onboarded app (MyBank, LoanWise)
#   3. Runs tests/run_sentinel_scenarios.py for the full multi-turn pipeline
#   4. Waits ~60s for Arize ingestion
#   5. Pulls /v1/metrics on port 3010 for each app and prints a digest

set -euo pipefail

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT_DIR="/Users/981061/Documents/Claude/Projects/RAI May manifestation/rai-trust-platform-prototype"
ASSURANCE_DIR="/Users/981061/Documents/Claude/Projects/RAI May manifestation/rai-assurance"
LOANAPP_DIR="/Users/981061/Documents/Claude/Projects/RAI May manifestation/rai-clients/loanapp"
PAP_VENV="$ROOT_DIR/venv/bin/activate"
ASSURANCE_VENV="$ASSURANCE_DIR/.venv/bin/activate"

# ── Expected port map (post-26d9ba0) ─────────────────────────────────────────
PORT_GUARDRAIL=3001
PORT_PAP=3002
PORT_PE=3003
PORT_ASSURANCE=3010
PORT_MYBANK=5000
PORT_LOANWISE=8002

# ── Output helpers ───────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; RESET='\033[0m'
ok()   { echo -e "  ${GREEN}[OK]${RESET} $*"; }
warn() { echo -e "  ${YELLOW}[!!]${RESET} $*"; }
err()  { echo -e "  ${RED}[XX]${RESET} $*"; }
hdr()  { echo; echo -e "${BOLD}>>  $*${RESET}"; }

# ── 1. Verify all 6 services are up ──────────────────────────────────────────
hdr "Step 1 — Verify all 6 services on expected ports"

MISSING=""
for spec in "Guardrail:$PORT_GUARDRAIL" "PAP:$PORT_PAP" "Policy Engine:$PORT_PE" \
            "Assurance:$PORT_ASSURANCE" "MyBank:$PORT_MYBANK" "LoanWise:$PORT_LOANWISE"; do
  name="${spec%%:*}"
  port="${spec##*:}"
  if lsof -nP -iTCP:$port -sTCP:LISTEN 2>/dev/null | grep -q LISTEN; then
    ok "$name listening on $port"
  else
    err "$name NOT listening on $port"
    MISSING+="$name($port) "
  fi
done

if [[ -n "$MISSING" ]]; then
  err "Missing services: $MISSING"
  warn "Start them in their terminals (see path_b_rebuild.sh output) then re-run."
  exit 1
fi

# ── 2. Verify both apps are onboarded ────────────────────────────────────────
hdr "Step 2 — Verify mybank + loanapp are onboarded in PAP"

DB="$ROOT_DIR/pap/config/rai_trust.db"
ONBOARDED=$(sqlite3 "$DB" "SELECT app_id FROM config_apps WHERE app_id IN ('mybank','loanapp');" 2>/dev/null | tr '\n' ' ')

for app in mybank loanapp; do
  if echo "$ONBOARDED" | grep -qw "$app"; then
    PACKS=$(sqlite3 "$DB" "SELECT pack_id FROM config_app_pack_assignments WHERE app_id='$app' AND active=1;" 2>/dev/null | tr '\n' ',' | sed 's/,$//')
    ok "$app onboarded — active packs: ${PACKS:-NONE}"
  else
    err "$app NOT onboarded via PAP UI"
    warn "Open http://localhost:$PORT_PAP/ui and onboard $app under App Stack tab"
    exit 1
  fi
done

# ── 3. Hydrate per-app scenarios ─────────────────────────────────────────────
hdr "Step 3 — Hydrate per-app scenarios (MyBank + LoanWise)"

# Use our calibrated_1k_corpus at small N to seed each app with weighted traffic
cd "$ASSURANCE_DIR"
# shellcheck disable=SC1090
source "$ASSURANCE_VENV"

echo "  Firing 30 prompts at MyBank (port $PORT_MYBANK)..."
python scripts/calibrated_1k_corpus.py --app mybank \
  --url "http://127.0.0.1:$PORT_MYBANK" \
  --count 30 --interval 2 --seed 42 2>&1 | tail -15 || warn "MyBank batch had errors"

echo
echo "  Firing 30 prompts at LoanWise (port $PORT_LOANWISE)..."
python scripts/calibrated_1k_corpus.py --app loanapp \
  --url "http://127.0.0.1:$PORT_LOANWISE" \
  --count 30 --interval 2 --seed 43 2>&1 | tail -15 || warn "LoanWise batch had errors"

# ── 4. Run Sharad's sentinel scenarios for the broader pipeline ──────────────
hdr "Step 4 — Run sentinel scenarios (full multi-turn pipeline)"

cd "$ROOT_DIR"
# shellcheck disable=SC1090
source "$PAP_VENV"

if [[ -f "tests/run_sentinel_scenarios.py" ]]; then
  python tests/run_sentinel_scenarios.py 2>&1 | tail -20 || warn "Sentinel scenarios had errors (acceptable if Sentinel agent isn't started yet)"
else
  warn "tests/run_sentinel_scenarios.py not found — skipping"
fi

# ── 5. Wait for Arize ingestion ──────────────────────────────────────────────
hdr "Step 5 — Wait 60s for Arize ingestion"

echo "  Arize typically takes 30-90 sec to surface new spans."
for i in {60..1}; do
  printf "\r  $i sec...  "
  sleep 1
done
echo

# ── 6. Smoke test /v1/metrics ────────────────────────────────────────────────
hdr "Step 6 — Smoke test /v1/metrics on port $PORT_ASSURANCE"

cd "$ASSURANCE_DIR"
# shellcheck disable=SC1090
source "$ASSURANCE_VENV"

for app in mybank loanapp; do
  echo
  echo "  --- $app (1h window) ---"
  curl -s --max-time 60 "http://localhost:$PORT_ASSURANCE/v1/metrics?app_id=$app&window=1h" \
    | python3 -c "
import json, sys
try:
  d = json.load(sys.stdin)
  print(f'  envelope: {d[\"data_status\"]}')
  print(f'  metric_count: {d[\"metric_count\"]}')
  print(f'  {\"metric_id\":<45} {\"value\":<10} {\"unit\":<8} status')
  print('  ' + '-' * 78)
  for m in d['metrics']:
      marker = ' ★' if m['data_status'] == 'experimental' else ''
      print(f'  {m[\"metric_id\"]:<45} {str(m[\"value\"]):<10} {m[\"unit\"]:<8} [{m[\"data_status\"]}]{marker}')
except Exception as e:
  print(f'  ERROR parsing response: {e}')
  sys.exit(1)
"
done

hdr "Done"
echo
echo "  If both apps show metric_count=21 and a mix of [ok] / [experimental] /"
echo "  [no_data] statuses, Path B is complete."
echo
echo "  Next: open http://localhost:$PORT_PAP/ui/dashboard and verify the cockpit"
echo "  renders with both apps selectable in APP SCOPE."
echo
