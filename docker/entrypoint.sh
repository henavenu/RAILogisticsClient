#!/usr/bin/env bash
set -euo pipefail

# ── Cloud Run / local entrypoint for the single-container RAI Trust Platform ──
# Cloud Run injects $PORT (the ONE externally-reachable port). nginx binds it and
# reverse-proxies to the internal services on loopback. All inter-service URLs are
# loopback because everything runs in this one container.

export PORT="${PORT:-8080}"

# Shared inter-service config. Each value is overridable by an env var supplied at
# deploy time (Cloud Run --set-env-vars / --set-secrets); these are the defaults
# for the single-container topology.
export AUDIT_SQLITE_PATH="${AUDIT_SQLITE_PATH:-/app/pap/config/pap.db}"
# The Guardrail Service prefers PAP_DB_PATH over AUDIT_SQLITE_PATH. Export it to the
# SAME shared DB so a stale host-side guardrail-service/.env (which may carry a
# Windows path) cannot win: python-dotenv's load_dotenv(override=False) keeps this
# entrypoint value, so guardrail reads the seeded pap.db instead of creating an
# empty one ("no such table: config_evaluation_dimensions").
export PAP_DB_PATH="${PAP_DB_PATH:-$AUDIT_SQLITE_PATH}"
export GUARDRAIL_SERVICE_URL="${GUARDRAIL_SERVICE_URL:-http://127.0.0.1:3001}"
export GUARDRAIL_URL="${GUARDRAIL_URL:-http://127.0.0.1:3001}"
export POLICY_ENGINE_URL="${POLICY_ENGINE_URL:-http://127.0.0.1:3003}"
export TRUST_PE_URL="${TRUST_PE_URL:-http://127.0.0.1:3003}"
export SENTINEL_URL="${SENTINEL_URL:-http://127.0.0.1:3012}"
export COORDINATOR_URL="${COORDINATOR_URL:-http://127.0.0.1:3013}"
export ASSURANCE_URL="${ASSURANCE_URL:-http://127.0.0.1:3010}"
# Ports (defaults match start_all.bat).
export GS_PORT="${GS_PORT:-3001}"; export PAP_PORT="${PAP_PORT:-3002}"
export PE_PORT="${PE_PORT:-3003}"; export SENTINEL_PORT="${SENTINEL_PORT:-3012}"
export COORDINATOR_PORT="${COORDINATOR_PORT:-3013}"
export DASHBOARD_PORT="${DASHBOARD_PORT:-3004}"

# ── Non-secret app config ─────────────────────────────────────────────────────
# The .env files are deliberately NOT baked into the image (see .dockerignore):
# they carry live secrets + host-only Windows paths. The entrypoint is therefore
# the single source of non-secret config so the container is self-contained and
# behaves identically to the local `start_all.bat` demo. Real secrets (GROQ /
# AZURE / AWS / GOOGLE_API_KEY) are injected at runtime: `docker run -e ...` or
# Cloud Run `--set-secrets`. Every value is overridable.

# PAP: pin the shared SQLite DB to pap/config/pap.db. WITHOUT this, PAP's code
# default is a DIFFERENT file (rai_trust.db), which the Policy Engine / Guardrail
# (reading AUDIT_SQLITE_PATH) would NOT see → "no such table". sqlite:///config/...
# resolves relative to /app/pap (PAP's cwd).
export PAP_DATABASE_URL="${PAP_DATABASE_URL:-sqlite:///config/pap.db}"
export ASSURANCE_PROVIDER="${ASSURANCE_PROVIDER:-mock}"

# CRITICAL: the shared pap.db lives on Docker's overlayfs, where SQLite WAL mode's
# mmap'd -shm shared-memory file is NOT supported across processes — concurrent
# writers (PAP + Policy Engine audit writers) corrupt it ("database disk image is
# malformed"). Force the overlayfs-safe rollback journal here. (Local dev on a
# native filesystem leaves the WAL default.) See pap/core/database.py.
export SQLITE_JOURNAL_MODE="${SQLITE_JOURNAL_MODE:-DELETE}"

# ── LLM provider (order: Google Vertex → Azure OpenAI → Groq → Ollama) ────────
# Selection is driven by which credential is present (see MyBank graph.py and the
# trust-agents). To use Google Gemini on GCP, supply GOOGLE_CLOUD_PROJECT at deploy
# (Cloud Run --set-env-vars) — Vertex auth is the service account (roles/aiplatform.user),
# no API key. LOCATION/MODEL default here so only the project id is required; both
# are overridable. GOOGLE_CLOUD_PROJECT is intentionally NOT defaulted — its presence
# is what selects Vertex.
export GOOGLE_CLOUD_LOCATION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
export GEMINI_MODEL="${GEMINI_MODEL:-gemini-2.5-flash}"

# Policy Engine behaviour (fail-closed; Sentinel webhook + A2A/HITL thresholds).
export FAIL_MODE="${FAIL_MODE:-closed}"
export SENTINEL_WEBHOOK_URL="${SENTINEL_WEBHOOK_URL:-http://127.0.0.1:3012/evaluate}"
export SENTINEL_SYNC_URL="${SENTINEL_SYNC_URL:-http://127.0.0.1:3012}"
export SENTINEL_TRIGGER_N="${SENTINEL_TRIGGER_N:-5}"
export A2A_HITL_THRESHOLD="${A2A_HITL_THRESHOLD:-5}"

# Sentinel + Coordinator agent-to-agent URLs. These are REQUIRED-env: both services
# fail closed (exit 1) if the required ones are missing — so they MUST be set here
# now that the .env files no longer ship in the image.
#   Sentinel  : SENTINEL_PE_URL (required) pulls audit events from the Policy Engine;
#               SENTINEL_COORDINATOR_URL (optional) enables auto-escalation.
#   Coordinator: COORDINATOR_SENTINEL_URL (required) reads Sentinel findings;
#               COORDINATOR_PAP_URL (required) writes governance alerts to PAP.
export SENTINEL_PE_URL="${SENTINEL_PE_URL:-http://127.0.0.1:3003}"
export SENTINEL_COORDINATOR_URL="${SENTINEL_COORDINATOR_URL:-http://127.0.0.1:3013}"
export COORDINATOR_SENTINEL_URL="${COORDINATOR_SENTINEL_URL:-http://127.0.0.1:3012}"
export COORDINATOR_PAP_URL="${COORDINATOR_PAP_URL:-http://127.0.0.1:3002}"

# trust-agents aggregator (Trust Radar / Trust Index data) — server-side loopback.
export TRUST_AGENTS_URL="${TRUST_AGENTS_URL:-http://127.0.0.1:3005}"

# ── BROWSER-facing service URLs (UI_*) ────────────────────────────────────────
# The dashboard JS dereferences these in the USER'S browser, which cannot reach
# the agent services on their loopback ports through the proxy. So we hand the
# dashboard SAME-ORIGIN proxy paths (nginx routes /agents/* → the loopback service).
# PAP's /v1/ui-config serves these to the dashboard. (In local dev these are unset
# and ui-config falls back to localhost:<port>, which the browser can reach.)
export UI_SENTINEL_URL="${UI_SENTINEL_URL:-/agents/sentinel}"
export UI_COORDINATOR_URL="${UI_COORDINATOR_URL:-/agents/coordinator}"
export UI_TRUST_URL="${UI_TRUST_URL:-/agents/trust}"
export STORE_CONTENT_SNIPPETS="${STORE_CONTENT_SNIPPETS:-true}"
export CONTENT_SNIPPET_MAX_CHARS="${CONTENT_SNIPPET_MAX_CHARS:-2000}"

# SDK governance enforcement, shared by BOTH apps (RAISE = enforce; HITL threshold).
# This image runs two instrumented apps against one governance stack:
#   PharmaLogistics  :5001  → served at /         (the demo's default app)
#   MyBank           :5000  → served at /mybank/
# Their PER-APP settings (TRUST_APP_ID, MYBANK_PORT, MYBANK_CALLBACK_URL) are NOT
# set here — a single global value would collapse both apps onto one identity. They
# are set per-process in docker/supervisord.conf, which overrides anything exported
# here. The TRUST_APP_ID below is only a fallback for any other process that reads it.
export TRUST_APP_ID="${TRUST_APP_ID:-pharmalogistics}"
export TRUST_EVAL_MODE="${TRUST_EVAL_MODE:-SYNC}"
export TRUST_ENFORCEMENT_MODE="${TRUST_ENFORCEMENT_MODE:-RAISE}"
export TRUST_HITL_THRESHOLD="${TRUST_HITL_THRESHOLD:-2}"
export PAP_URL="${PAP_URL:-http://127.0.0.1:3002}"
export MYBANK_COUNCIL_MODE="${MYBANK_COUNCIL_MODE:-real}"
export MYBANK_COUNCIL_CONCURRENT="${MYBANK_COUNCIL_CONCURRENT:-true}"
# NOTE: OTEL_EXPORTER_OTLP_ENDPOINT is intentionally left UNSET — there is no OTLP
# collector in this image, and setting it would make the SDK attempt (and log)
# failed exports to localhost:4317. Set it at runtime if you wire up tracing.

mkdir -p "$(dirname "$AUDIT_SQLITE_PATH")"

# DB init from the committed snapshot — the SAME tools/seed_local.py that local dev
# (start_all.bat) runs, so container and local initialise IDENTICALLY. It copies
# pap.seed.db → pap.db when the snapshot changed (and scrubs stale WAL/SHM sidecars
# that would otherwise read as "database disk image is malformed"), and preserves
# pap.db otherwise. On Cloud Run's ephemeral FS pap.db is absent each cold start, so
# this re-seeds fresh config every time. The snapshot is the schema+config source of
# truth — init_db()/alembic never (re)builds the schema (which drifts).
rm -f /app/pap/config/pap.seed.db-wal /app/pap/config/pap.seed.db-shm 2>/dev/null || true
python3 /app/tools/seed_local.py || echo "[entrypoint] WARNING: DB init (seed_local.py) failed"

# Render the external port into the nginx config (Cloud Run's $PORT is dynamic).
sed "s/__PORT__/${PORT}/g" /etc/nginx/nginx.conf.tmpl > /etc/nginx/nginx.conf

echo "[entrypoint] starting — external PORT=${PORT}, DB=${AUDIT_SQLITE_PATH}"
echo "[entrypoint] apps: PharmaLogistics at /   MyBank at /mybank/   dashboard at /ui/dashboard"
echo "[entrypoint] LLM provider order: gemini -> azure-openai -> groq -> ollama (whichever key is present)"

# supervisord runs nginx + all services. Service start order (priorities) and the
# wait-for-:3002 gate (in supervisord.conf) reproduce start_all.bat: PAP seeds the
# DB before binding :3002, Policy Engine waits for that, MyBank waits for the PE.
exec supervisord -c /etc/supervisor/conf.d/rai.conf
