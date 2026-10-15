#!/usr/bin/env bash
# NeuroTrust VitaLink — standalone launcher (macOS / Linux), port 5001.
# The app is a client of the Trust platform: it installs the RAI Trust SDK from
# sdk-dist/wheelhouse and reaches the Trust APIs over HTTP. Which platform it talks
# to (local beta / GCP) is set by RAI_PE_URL + RAI_PAP_URL in rai.env and
# RAI_APP_KEY in .env — no platform code lives in this repo.
set -euo pipefail
cd "$(dirname "$0")"

export MYBANK_PORT="${MYBANK_PORT:-5001}"

VENV="./venv"
PY="$VENV/bin/python"

# Deps (langchain-core 1.4.8 etc.) need Python >=3.10; macOS system python3 is often
# 3.9, so prefer a newer interpreter. Override with PYTHON=/path/to/python.
pick_python() {
  if [ -n "${PYTHON:-}" ]; then echo "$PYTHON"; return; fi
  for c in python3.12 python3.13 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && \
       "$c" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,10) else 1)' 2>/dev/null; then
      echo "$c"; return
    fi
  done
  echo ""
}

if [ ! -x "$PY" ]; then
  BOOT_PY="$(pick_python)"
  if [ -z "$BOOT_PY" ]; then
    echo "[VitaLink] FATAL: no Python >=3.10 found. Install one (e.g. 'brew install python@3.12') or set PYTHON=/path/to/python." >&2
    exit 1
  fi
  echo "[VitaLink] First run — creating venv with $BOOT_PY and installing dependencies..."
  "$BOOT_PY" -m venv "$VENV"
  "$PY" -m pip install --upgrade pip --quiet
  # requirements.txt pins the RAI Trust SDK, which resolves from the bundled wheelhouse.
  "$PY" -m pip install --find-links sdk-dist/wheelhouse -r requirements.txt
fi

[ -f rai.env ] || { echo "[VitaLink] No rai.env — copying rai.env.example (Trust API URLs)."; cp rai.env.example rai.env; }
[ -f .env ]    || { echo "[VitaLink] No .env — copying .env.example. Set your LLM key and RAI_APP_KEY."; cp .env.example .env; }

# Local Trust stack only: start the gateway on :3080, which gives the dashboard's Human Review
# tab the routing the GKE ingress provides (served straight from :3004 the queue looks empty).
# On GCP the ingress already does this, so it is skipped when rai.env points elsewhere.
PAP_CFG="$(grep -E '^RAI_PAP_URL=' rai.env | cut -d= -f2 | awk '{print $1}')"
if [[ "$PAP_CFG" =~ ^http://(127\.0\.0\.1|localhost)[:/] ]]; then
  if ! lsof -nP -iTCP:3080 -sTCP:LISTEN >/dev/null 2>&1; then
    nohup "$PY" trust_gateway.py > gateway.log 2>&1 &
    sleep 2
  fi
  if curl -fs -o /dev/null http://127.0.0.1:3080/v1/hitl; then
    echo "[VitaLink] Human Review: http://127.0.0.1:3080/ui/dashboard?goto=hitl  (use :3080, not :3004)"
  else
    echo "[VitaLink] WARNING: Human Review gateway on :3080 is not answering. Held requests will not show" >&2
    echo "[VitaLink]          in the dashboard. Check gateway.log (is the Trust platform up?)." >&2
  fi
fi

echo "[VitaLink] Starting on http://127.0.0.1:${MYBANK_PORT}  (app_id=pharmalogistics)"
exec "$PY" server.py
