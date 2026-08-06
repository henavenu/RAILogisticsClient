#!/usr/bin/env bash
# RAI Trust Platform — Start All Services (macOS/Linux port of start_all.bat)
#
# Service map:
#   3001  Guardrail Service
#   3002  PAP (Policy Admin Platform + Web UI + Dashboard)
#   3003  Policy Engine
#   3010  Mock Assurance Provider  (launched only when ASSURANCE_URL points at it)
#   3012  Sentinel Agent
#   3013  Coordinator Agent
#   5000  myBank Application
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3.12}"
LOGDIR="$ROOT/.launch-logs/run"
mkdir -p "$LOGDIR"

say() { printf '%s\n' "$*"; }

# -- Fresh-clone bootstrap: create missing .env files from .env.example --------
ENV_CREATED=0
for D in "." "pap" "policy-engine" "guardrail-service" "sdk/examples/mybank_clean" \
         "trust-agents" "trust-agents/sentinel" "trust-agents/coordinator"; do
  if [ -f "$D/.env.example" ] && [ ! -f "$D/.env" ]; then
    cp "$D/.env.example" "$D/.env"
    say "[setup] created $D/.env from .env.example"
    ENV_CREATED=1
  fi
done
[ "$ENV_CREATED" = "1" ] && say "[setup] >>> New .env files created. Add secret keys (GROQ_API_KEY, etc.) for live LLM calls."

# -- Root virtual environment --------------------------------------------------
if [ ! -x "$ROOT/venv/bin/python" ]; then
  say "[Setup] Creating root venv with $PYTHON ..."
  "$PYTHON" -m venv "$ROOT/venv"
  "$ROOT/venv/bin/python" -m pip install --upgrade pip --quiet
  say "[Setup] Installing root requirements (one-time)..."
  "$ROOT/venv/bin/pip" install -r requirements.txt
else
  say "[Setup] Root venv found."
fi
VENV_PY="$ROOT/venv/bin/python"
VENV_UVICORN="$ROOT/venv/bin/uvicorn"

# -- DB init: materialise pap.db from the committed snapshot --------------------
"$VENV_PY" tools/seed_local.py || say "[setup] WARNING: DB init failed"

# -- Guard: ensure Guardrail Service points at the real pap.db -----------------
# Fresh clones ship guardrail-service/.env with a placeholder PAP_DB_PATH
# (/absolute/path/to/pap.db). If it is set but points at a missing file, GS
# can't load its evaluation taxonomy -> /v1/health goes 503 -> PE fails open.
# Self-heal by rewriting it to the local DB. A *blank* value is intentional
# (YAML-fallback mode) and is left untouched.
GS_ENV="$ROOT/guardrail-service/.env"
REAL_DB="$ROOT/pap/config/pap.db"
if [ -f "$GS_ENV" ]; then
  cur_db="$(sed -n 's/^PAP_DB_PATH=//p' "$GS_ENV" | head -1)"
  if [ -n "$cur_db" ] && [ ! -f "$cur_db" ]; then
    sed -i.bak "s|^PAP_DB_PATH=.*|PAP_DB_PATH=$REAL_DB|" "$GS_ENV" && rm -f "$GS_ENV.bak"
    say "[setup] Guardrail .env PAP_DB_PATH was invalid ('$cur_db') — repaired to $REAL_DB"
  fi
fi

# -- Correctness fixes / shared env --------------------------------------------
export AUDIT_SQLITE_PATH="$ROOT/pap/config/pap.db"
export GUARDRAIL_SERVICE_URL="http://127.0.0.1:3001"
export POLICY_ENGINE_URL="http://127.0.0.1:3003"

# -- Load root .env (user-tunable runtime config) ------------------------------
if [ -f ".env" ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

# -- Mock Assurance proxy decision ---------------------------------------------
ASSURANCE_URL="${ASSURANCE_URL:-http://127.0.0.1:3010}"
LAUNCH_PROXY=0
case "$ASSURANCE_URL" in
  http://127.0.0.1:3010|http://localhost:3010) LAUNCH_PROXY=1 ;;
esac

# -- Pre-flight: release stale sockets on all service ports --------------------
say ""
say "[Pre-flight] Releasing stale sockets on service ports..."
for p in 3001 3002 3003 3010 3012 3013 5000; do
  pids=$(lsof -ti tcp:"$p" 2>/dev/null || true)
  [ -n "$pids" ] && { say "[Pre-flight] Killing $pids on :$p"; kill -9 $pids 2>/dev/null || true; }
done
sleep 2
say "[Pre-flight] Done."
say ""

# launch <name> <port> <workdir> <logfile> -- cmd...
launch() {
  local name="$1" port="$2" wd="$3" log="$4"; shift 4
  say "[start] $name on :$port"
  ( cd "$wd" && exec "$@" ) >"$LOGDIR/$log" 2>&1 &
  echo $! >>"$LOGDIR/pids"
}

wait_for_port() {
  local port="$1" limit="${2:-60}" n=0
  while [ "$n" -lt "$limit" ]; do
    if lsof -ti tcp:"$port" >/dev/null 2>&1; then say "[wait] :$port ready."; return 0; fi
    sleep 2; n=$((n+2))
  done
  say "[wait] WARNING: :$port not up in ${limit}s — proceeding anyway"
}

: >"$LOGDIR/pids"

# -- Mock Assurance Provider (optional) ----------------------------------------
if [ "$LAUNCH_PROXY" = "1" ]; then
  launch "Mock Assurance" 3010 "$ROOT/pap/tools/proxy_assurance" "assurance.log" \
    "$VENV_UVICORN" proxy_assurance:app --host 0.0.0.0 --port 3010 --log-level info
  sleep 2
fi

# -- UI / Admin cluster --------------------------------------------------------
launch "PAP (Dashboard)" 3002 "$ROOT/pap" "pap.log" "$VENV_PY" main.py
sleep 2

# -- Governance: Guardrail starts in parallel while PAP boots -------------------
launch "Guardrail Service" 3001 "$ROOT/guardrail-service" "guardrail.log" "$VENV_PY" main.py

# -- Wait for PAP (DB init) before Policy Engine -------------------------------
say "[wait] Waiting for PAP DB init on :3002 (up to 60s)..."
wait_for_port 3002 60

launch "Policy Engine" 3003 "$ROOT/policy-engine" "policy-engine.log" "$VENV_PY" main.py
sleep 3

# -- Trust Agents --------------------------------------------------------------
launch "Sentinel" 3012 "$ROOT/trust-agents/sentinel" "sentinel.log" "$VENV_PY" main.py
sleep 2
launch "Coordinator" 3013 "$ROOT/trust-agents/coordinator" "coordinator.log" "$VENV_PY" main.py
sleep 2

# -- myBank Application (separate venv) ----------------------------------------
MB="$ROOT/sdk/examples/mybank_clean"
if [ ! -x "$MB/venv/bin/python" ]; then
  say "[myBank] First run — creating local venv and installing dependencies (~1-2 min)..."
  "$PYTHON" -m venv "$MB/venv"
  "$MB/venv/bin/python" -m pip install --upgrade pip --quiet
  "$MB/venv/bin/pip" install -r "$MB/requirements.txt"
  # In-repo packages (order matters: sdk before sdk-langgraph)
  "$MB/venv/bin/pip" install "$ROOT/privacy-proxy" "$ROOT/sdk" "$ROOT/sdk-langgraph"
fi
say "[start] myBank on :5000"
(
  cd "$MB"
  export MYBANK_CALLBACK_URL="http://127.0.0.1:5000"
  export PAP_URL="http://127.0.0.1:3002"
  export SENTINEL_URL="http://127.0.0.1:3012"
  exec "$MB/venv/bin/python" server.py
) >"$LOGDIR/mybank.log" 2>&1 &
echo $! >>"$LOGDIR/pids"
sleep 3

# -- Verify --------------------------------------------------------------------
say ""
say "[Verify] Checking service ports..."
# port:name pairs (bash 3.2 compatible — macOS default shell has no associative arrays)
for entry in "3001:Guardrail" "3002:PAP" "3003:Policy Engine" "3010:Mock Assurance" \
             "3012:Sentinel" "3013:Coordinator" "5000:myBank"; do
  p="${entry%%:*}"; nm="${entry#*:}"
  if lsof -ti tcp:"$p" >/dev/null 2>&1; then say "[UP  ] :$p  $nm"; else say "[WAIT] :$p  $nm (still starting — see $LOGDIR/)"; fi
done

cat <<EOF

============================================================
 All services launched!

 UI / Admin
   PAP + Dashboard  : http://127.0.0.1:3002/ui
   Trust Radar      : http://127.0.0.1:3002/ui/dashboard
   PAP API docs     : http://127.0.0.1:3002/docs

 Governance
   Guardrail Svc    : http://127.0.0.1:3001/docs
   Policy Engine    : http://127.0.0.1:3003/docs

 Trust Agents
   Sentinel         : http://127.0.0.1:3012/docs
   Coordinator      : http://127.0.0.1:3013/docs
   Mock Assurance   : http://127.0.0.1:3010/v1/docs

 Applications
   myBank           : http://127.0.0.1:5000

 Logs: $LOGDIR/    (stop with ./stop_all.sh)
============================================================
EOF
