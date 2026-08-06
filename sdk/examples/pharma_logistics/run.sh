#!/usr/bin/env bash
# NeuroTrust PharmaLogistics — local launcher (macOS / Linux), port 5001.
# First run provisions a local venv (PyPI requirements + the three in-repo packages:
# privacy-proxy, sdk, sdk-langgraph — the same set the reference app installs).
set -euo pipefail
cd "$(dirname "$0")"

# Pin service URLs so they can't be silently overridden by inherited env.
export MYBANK_CALLBACK_URL="${MYBANK_CALLBACK_URL:-http://127.0.0.1:5001}"
export MYBANK_PORT="${MYBANK_PORT:-5001}"
export PAP_URL="${PAP_URL:-http://127.0.0.1:3002}"
export SENTINEL_URL="${SENTINEL_URL:-http://127.0.0.1:3012}"
export TRUST_PE_URL="${TRUST_PE_URL:-http://127.0.0.1:3003}"

VENV="./venv"
PY="$VENV/bin/python"

# Pick a Python >=3.10 for the venv. Deps (langchain-core 1.4.8 etc.) require 3.10+,
# and macOS system `python3` is often 3.9 — so prefer a newer interpreter if present.
# Override with PYTHON=/path/to/python.
pick_python() {
  if [ -n "${PYTHON:-}" ]; then echo "$PYTHON"; return; fi
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && \
       "$c" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,10) else 1)' 2>/dev/null; then
      echo "$c"; return
    fi
  done
  echo ""  # none found
}

if [ ! -x "$PY" ]; then
  BOOT_PY="$(pick_python)"
  if [ -z "$BOOT_PY" ]; then
    echo "[PharmaLogistics] FATAL: no Python >=3.10 found. Install one (e.g. 'brew install python@3.12') or set PYTHON=/path/to/python." >&2
    exit 1
  fi
  echo "[PharmaLogistics] First run — creating local venv with $BOOT_PY ($("$BOOT_PY" --version 2>&1)) and installing dependencies (~1-2 min)..."
  "$BOOT_PY" -m venv "$VENV"
  "$PY" -m pip install --upgrade pip --quiet
  "$PY" -m pip install -r requirements.txt
  # In-repo packages from the repo root. Order matters: sdk before sdk-langgraph.
  "$PY" -m pip install ../../../privacy-proxy ../../../sdk ../../../sdk-langgraph
fi

if [ ! -f ".env" ]; then
  echo "[PharmaLogistics] No .env found — copying .env.example. Edit it to set your LLM provider."
  cp .env.example .env
fi

echo "[PharmaLogistics] Starting on http://127.0.0.1:${MYBANK_PORT}  (app_id=pharmalogistics)"
exec "$PY" server.py
