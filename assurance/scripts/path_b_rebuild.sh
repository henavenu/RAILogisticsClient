#!/usr/bin/env bash
# Path B: DB rebuild for May18 schema migration.
#
# What this does:
#   1. Checks all DB-touching services are stopped (warns + exits if running)
#   2. Backs up rai_trust.db with timestamp suffix
#   3. Removes the live rai_trust.db
#   4. Runs pap/bootstrap.py to recreate schema + seed config from Sharad's May18 master
#
# What this does NOT do (manual after this script):
#   • Launch services (Guardrail, PE, PAP, Assurance, MyBank, LoanWise)
#   • Onboard apps via PAP UI
#   • Re-hydrate evaluation data (use path_b_hydrate_test.sh for that)
#
# Safety: backup is created BEFORE the live DB is deleted. To roll back:
#   cp pap/config/rai_trust.db.backup-<timestamp> pap/config/rai_trust.db

set -euo pipefail

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT_DIR="/Users/981061/Documents/Claude/Projects/RAI May manifestation/rai-trust-platform-prototype"
PAP_VENV="$ROOT_DIR/venv/bin/activate"
DB_PATH="$ROOT_DIR/pap/config/rai_trust.db"
BOOTSTRAP="$ROOT_DIR/pap/bootstrap.py"

# ── Output helpers ───────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; RESET='\033[0m'
ok()   { echo -e "  ${GREEN}[OK]${RESET} $*"; }
warn() { echo -e "  ${YELLOW}[!!]${RESET} $*"; }
err()  { echo -e "  ${RED}[XX]${RESET} $*"; }
hdr()  { echo; echo -e "${BOLD}>>  $*${RESET}"; }

# ── 0. Pre-flight ────────────────────────────────────────────────────────────
hdr "Pre-flight checks"

if [[ ! -f "$PAP_VENV" ]]; then
  err "Prototype venv not found at $PAP_VENV"
  exit 1
fi
ok "Prototype venv exists"

if [[ ! -f "$BOOTSTRAP" ]]; then
  err "bootstrap.py not found at $BOOTSTRAP"
  exit 1
fi
ok "bootstrap.py exists"

# ── 1. Services-stopped check ────────────────────────────────────────────────
# Path B requires PE/PAP/Guardrail to release their DB locks before we can
# safely delete the file. Also Assurance/MyBank/LoanWise should be down so
# they don't fire requests at services that aren't there.
hdr "Step 1 — Verify all services are stopped"

PORTS_TO_CHECK="3000 3001 3002 3003 3010 8000 8001 8002 5000"
RUNNING=""
for p in $PORTS_TO_CHECK; do
  if lsof -nP -iTCP:$p -sTCP:LISTEN 2>/dev/null | grep -q LISTEN; then
    RUNNING+="$p "
  fi
done

if [[ -n "$RUNNING" ]]; then
  err "Services still running on port(s): $RUNNING"
  warn "Press Ctrl+C in each service terminal, then re-run this script."
  exit 1
fi
ok "No services listening on tracked ports"

# ── 2. Backup ────────────────────────────────────────────────────────────────
hdr "Step 2 — Backup current DB"

if [[ ! -f "$DB_PATH" ]]; then
  warn "DB not found at $DB_PATH — nothing to backup. Proceeding to bootstrap."
else
  STAMP=$(date +%Y%m%d-%H%M%S)
  BACKUP="${DB_PATH}.backup-pre-may18-${STAMP}"
  cp "$DB_PATH" "$BACKUP"
  ok "Backup written: ${BACKUP##*/}"
  ok "  (full path: $BACKUP)"
fi

# ── 3. Nuke live DB ──────────────────────────────────────────────────────────
hdr "Step 3 — Remove live DB"

if [[ -f "$DB_PATH" ]]; then
  rm "$DB_PATH"
  ok "Live DB removed"
else
  warn "Live DB already absent"
fi

# ── 4. Bootstrap ─────────────────────────────────────────────────────────────
hdr "Step 4 — Run bootstrap.py (recreate schema + seed config)"

cd "$ROOT_DIR"
# shellcheck disable=SC1090
source "$PAP_VENV"

# bootstrap.py prints its own banner + per-step status. Let it speak.
python pap/bootstrap.py

# ── 5. Verify new DB ─────────────────────────────────────────────────────────
hdr "Step 5 — Verify new DB has May18 schema"

if [[ ! -f "$DB_PATH" ]]; then
  err "bootstrap.py exited but DB not created at $DB_PATH"
  exit 1
fi
ok "New DB created"

# 0015 governance_alerts should exist now
if sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM governance_alerts;" >/dev/null 2>&1; then
  ok "governance_alerts table exists (0015 ✓)"
else
  warn "governance_alerts table missing — bootstrap may not include all migrations"
fi

# 0014 rename: should have sensitive_data_disclosure, NOT data_leakage
DIM_NAMES=$(sqlite3 "$DB_PATH" "SELECT dimension_id FROM config_evaluation_dimensions WHERE dimension_id IN ('data_leakage','sensitive_data_disclosure','pii_exposure_flag','data_minimisation_violation');" 2>/dev/null || echo "")
if echo "$DIM_NAMES" | grep -q "sensitive_data_disclosure"; then
  ok "sensitive_data_disclosure dimension present (0014 ✓)"
elif echo "$DIM_NAMES" | grep -q "data_leakage"; then
  warn "Still seeing OLD dimension name 'data_leakage' — May18 rename may not be in seed data"
else
  warn "No PII-related dimensions found in seed data (could be OK)"
fi

# ── 6. Next steps ────────────────────────────────────────────────────────────
hdr "Done — manual next steps before running path_b_hydrate_test.sh"

cat <<'INSTRUCTIONS'

Now start the 6 services in SEPARATE terminals, in this order:

  Terminal: Guardrail (3001)
    cd "/Users/981061/Documents/Claude/Projects/RAI May manifestation/rai-trust-platform-prototype/guardrail-service" && source ../venv/bin/activate && python main.py

  [wait ~4 seconds]

  Terminal: Policy Engine (3003)
    cd "/Users/981061/Documents/Claude/Projects/RAI May manifestation/rai-trust-platform-prototype/policy-engine" && source ../venv/bin/activate && python main.py

  [wait ~3 seconds]

  Terminal: PAP Dashboard (3002)
    cd "/Users/981061/Documents/Claude/Projects/RAI May manifestation/rai-trust-platform-prototype/pap" && source ../venv/bin/activate && python main.py

  Then in browser open: http://localhost:3002/ui
  Onboard via App Stack tab:
    - mybank     → sanctions + session_guard, enforcement=BLOCK
    - loanapp    → sanctions + session_guard, enforcement=BLOCK

  Terminal: Assurance API (3010 — new port)
    cd "/Users/981061/Documents/Claude/Projects/RAI May manifestation/rai-assurance" && source .venv/bin/activate && EVAL_FAST_MODE=true uvicorn api.main:app --port 3010 --reload

  Terminal: MyBank (5000 — new default)
    cd "/Users/981061/Documents/Claude/Projects/RAI May manifestation/rai-trust-platform-prototype/sdk/examples/mybank_clean" && source venv/bin/activate && python server.py

  Terminal: LoanWise (8002 — unchanged)
    cd "/Users/981061/Documents/Claude/Projects/RAI May manifestation/rai-clients/loanapp" && source venv/bin/activate && python server.py

When all 6 are up + both apps onboarded, run:
    bash scripts/path_b_hydrate_test.sh

INSTRUCTIONS
