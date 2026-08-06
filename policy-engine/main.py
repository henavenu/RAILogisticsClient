"""
Policy Engine — entry point.
Port: 3003 (PE_PORT; called by the RAI SDK)

Startup sequence:
  1. Init PE-owned DB tables (pe_sessions, pe_session_summaries, pe_app_trust_scores)
  2. Load policy definitions (DB-first, YAML fallback)
  3. Fetch deferred dimensions from Guardrail Service (GET /v1/evaluations)
     — falls back to an empty set if Guardrail Service is not yet running
  4. Construct PolicyOrchestrator (injected dependencies)
  5. Start FastAPI with /v1/app/evaluate, /v1/policies, /v1/health
"""
import io
import os
import sys
import logging
import logging.handlers

import httpx
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI

# Layered .env loading:
#   1. Service-level  policy-engine/.env  — orchestration config, FAIL_MODE
#   2. Root           rai-trust-platform/.env  — shared platform defaults (fallback only)
# Service-level values win; root fills any gaps not set at service level.
_svc_root = Path(__file__).parent
load_dotenv(_svc_root / ".env")                      # service-level (wins)
load_dotenv(_svc_root.parent / ".env")               # root fallback
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from core.policy_loader              import PolicyRegistry
from core.database                   import DB_AVAILABLE, init_pe_tables
from engine.orchestrator             import PolicyOrchestrator
from engine.pack_loader              import PackLoader
from engine.pack_manager             import PackManager
from engine.context_classifier       import ContextClassifier
from engine.chain_executor           import ChainExecutor
from engine.app_stack_manager        import DbAppStackManager
from engine.app_trust_tracker        import AppTrustTracker, DbAppTrustTracker
from engine.session_attack_detector  import SessionAttackDetector
from api.routes                      import create_router
from audit.writer                    import CompositeAuditWriter
from audit.console_writer            import ConsoleAuditWriter
from audit.jsonl_writer              import JsonlAuditWriter
from audit.sqlite_writer             import SqliteAuditWriter
from audit.hitl_writer               import HitlAlertWriter
from audit.governance_alerts_writer  import GovernanceAlertsWriter
from audit.sentinel_webhook          import SentinelWebhookForwarder
from audit.sentinel_sync_client      import SentinelSyncClient
from audit.reader                    import AuditReader
from session.store                   import InMemorySessionStore, DiskSessionStore, SqliteSessionStore

# ── Logging ─────────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
root = logging.getLogger()
root.setLevel(logging.DEBUG)
root.handlers.clear()

fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")

# Force UTF-8 on the console stream so emoji render correctly on Windows
_stdout_utf8 = (
    io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "buffer") else sys.stdout
)
ch = logging.StreamHandler(_stdout_utf8)
ch.setLevel(logging.INFO)
ch.setFormatter(fmt)
root.addHandler(ch)

fh = logging.handlers.RotatingFileHandler(
    "logs/policy-engine.log", maxBytes=10 * 1024 * 1024, backupCount=5
)
fh.setLevel(logging.DEBUG)
fh.setFormatter(fmt)
root.addHandler(fh)

logger = logging.getLogger(__name__)

# ── PE-owned DB tables ───────────────────────────────────────────────────────
# Create pe_sessions, pe_session_summaries, pe_app_trust_scores in the shared
# PAP database if they do not yet exist.  Safe to call on every startup.
if DB_AVAILABLE:
    try:
        init_pe_tables()
        logger.info("PE-owned DB tables ensured (pe_sessions, pe_session_summaries, pe_app_trust_scores)")
    except Exception as _e:
        logger.warning(f"init_pe_tables failed: {_e} — continuing without DB tables")

# ── Required-config validation ────────────────────────────────────────────────
# Peer-service URLs have NO silent fallback. A "fallback" to http://localhost:PORT
# is not a fallback — there is no alternate service there — it just disguises a
# missing config and then silently fails-open to PERMISSIVE. So a missing required
# var makes the service refuse to start with a clear error instead. (The ONLY
# legitimate fallback in this platform is the LLM provider chain.)
_MISSING_ENV: list = []

def _require_env(name: str, why: str = "") -> str:
    val = os.getenv(name, "").strip()
    if not val:
        _MISSING_ENV.append(f"{name}" + (f"  — {why}" if why else ""))
    return val

def _abort_if_missing_env() -> None:
    if _MISSING_ENV:
        logger.critical(
            "FATAL: required configuration missing — refusing to start.\n"
            "Set these (environment / .env — there is no localhost fallback):\n  - "
            + "\n  - ".join(_MISSING_ENV)
        )
        raise SystemExit(1)

# ── Configuration ────────────────────────────────────────────────────────────
# REQUIRED — the PE cannot score anything without the Guardrail Service.
GUARDRAIL_URL      = _require_env("GUARDRAIL_SERVICE_URL",
                                  "Policy Engine scores every dimension via the Guardrail Service")
HTTP_TIMEOUT       = float(os.getenv("GUARDRAIL_TIMEOUT_S", "10"))   # tuning default — OK
# Governance posture. Default CLOSED: if the Guardrail Service is unreachable,
# requests are BLOCKED (COERCIVE) rather than silently passed. Set FAIL_MODE=open
# only for non-production demos that must stay up during a guardrail outage.
FAIL_MODE          = os.getenv("FAIL_MODE", "closed").lower().strip()
_abort_if_missing_env()
# Chain executor feature flag — set ENABLE_PACK_ENGINE=true to activate
# the layered pack evaluation model (mandatory + context packs).
# Default: true.  Set to false to fall back to flat single-layer MRA.
ENABLE_PACK_ENGINE = os.getenv("ENABLE_PACK_ENGINE", "true").lower().strip() == "true"

# ── Policy Registry ──────────────────────────────────────────────────────────
try:
    policy_registry = PolicyRegistry()
    logger.info(f"PolicyRegistry loaded: {len(policy_registry.as_catalog())} definitions")
except Exception as e:
    logger.critical(f"Failed to load policy definitions: {e}")
    sys.exit(1)

# ── Deferred Dimensions ───────────────────────────────────────────────────────
# Fetch from Guardrail Service at startup so MRA exclusions stay in sync.
# Falls back to a safe empty set if Guardrail Service is unavailable.
deferred_dims: set = set()
try:
    r = httpx.get(f"{GUARDRAIL_URL}/v1/evaluations", timeout=5.0)
    r.raise_for_status()
    catalog = r.json().get("evaluations", [])
    deferred_dims = {e["dimension"] for e in catalog if e.get("status") == "deferred"}
    logger.info(f"Deferred dimensions fetched: {sorted(deferred_dims)}")
except Exception as e:
    logger.warning(
        f"Could not fetch deferred dimensions from Guardrail Service ({e}). "
        f"MRA will proceed with empty deferred set — all returned dims will contribute to verdict."
    )

# ── Pack Engine (ChainExecutor) ───────────────────────────────────────────────
# Constructs the layered policy chain from DB.  AUDIT_SQLITE_PATH must be set.
# Injected into the orchestrator only when ENABLE_PACK_ENGINE=true.
# Falls back gracefully to flat MRA if the DB is unreachable at startup.
chain_executor    = None
_pack_manager     = None
_pack_loader      = None
_app_stack_manager = None
if ENABLE_PACK_ENGINE:
    try:
        _pack_loader       = PackLoader()
        _pack_manager      = PackManager(_pack_loader, "")
        _classifier        = ContextClassifier()
        _app_stack_manager = DbAppStackManager()
        logger.info("AppStackManager: using DB-backed DbAppStackManager")

        chain_executor     = ChainExecutor(
            pack_loader=_pack_loader,
            classifier=_classifier,
            policy_registry=policy_registry,
            app_stack_manager=_app_stack_manager,
        )
        logger.info(
            f"Pack engine ACTIVE — "
            f"{len(_pack_loader.mandatory_packs())} mandatory pack(s), "
            f"{len(_pack_loader.layer2_packs())} Layer 2 pack(s)"
        )
    except Exception as e:
        logger.error(
            f"Pack engine failed to initialise: {e}. "
            f"Falling back to flat MRA evaluation."
        )
else:
    logger.info("Pack engine DISABLED — ENABLE_PACK_ENGINE=false (flat MRA mode)")

# ── Fail-mode banner ─────────────────────────────────────────────────────────
# Logged at WARNING level so it is always visible regardless of log filter.
_FAIL_MODE_VALID = FAIL_MODE in ("open", "closed")
if not _FAIL_MODE_VALID:
    logger.warning(f"Unknown FAIL_MODE='{FAIL_MODE}' — defaulting to 'closed' (safe)")
    FAIL_MODE = "closed"

logger.warning("=" * 60)
if FAIL_MODE == "closed":
    logger.warning("  FAIL MODE : CLOSED")
    logger.warning("  When the Guardrail Service is unreachable, ALL requests")
    logger.warning("  will be BLOCKED with verdict=COERCIVE.")
    logger.warning("  Suitable for: production, customer-facing applications.")
else:
    logger.warning("  FAIL MODE : OPEN")
    logger.warning("  When the Guardrail Service is unreachable, requests will")
    logger.warning("  PASS with verdict=PERMISSIVE (unguarded).")
    logger.warning("  WARNING: not suitable for production environments.")
    logger.warning("  Set FAIL_MODE=closed in .env for production.")
logger.warning("=" * 60)

# ── Session Store ─────────────────────────────────────────────────────────────
# Defaults to 'sqlite' when the shared PAP DB is available; otherwise 'memory'.
# Override with SESSION_STORE_TYPE env var.
_DEFAULT_SESSION_STORE = "sqlite" if DB_AVAILABLE else "memory"
SESSION_STORE_TYPE = os.getenv("SESSION_STORE_TYPE", _DEFAULT_SESSION_STORE).lower().strip()
SESSION_TTL_S      = float(os.getenv("SESSION_TTL_S", "1800"))   # 30 min default
SESSION_DISK_PATH  = os.getenv("SESSION_DISK_PATH",
                               str(Path(__file__).parent / "sessions"))

if SESSION_STORE_TYPE == "sqlite" and DB_AVAILABLE:
    session_store = SqliteSessionStore(ttl_seconds=SESSION_TTL_S)
    logger.info(f"Session store : SQLITE (PAP DB)  ttl={SESSION_TTL_S}s")
elif SESSION_STORE_TYPE == "disk":
    session_store = DiskSessionStore(store_dir=SESSION_DISK_PATH, ttl_seconds=SESSION_TTL_S)
    logger.info(f"Session store : DISK  path={SESSION_DISK_PATH}  ttl={SESSION_TTL_S}s")
else:
    if SESSION_STORE_TYPE == "sqlite" and not DB_AVAILABLE:
        logger.warning(
            "SESSION_STORE_TYPE=sqlite but AUDIT_SQLITE_PATH is not set — "
            "falling back to in-memory store"
        )
    session_store = InMemorySessionStore(ttl_seconds=SESSION_TTL_S)
    logger.info(f"Session store : MEMORY  ttl={SESSION_TTL_S}s")

# ── App Trust Tracker ────────────────────────────────────────────────────────
# Use DB-backed tracker when available so scores survive PE restarts and are
# visible to the PAP dashboard without an HTTP call.
if DB_AVAILABLE:
    app_trust_tracker = DbAppTrustTracker()
    logger.info("AppTrustTracker: using DB-backed DbAppTrustTracker (pe_app_trust_scores)")
else:
    _APP_TRUST_PATH = Path(__file__).parent / "data" / "app_trust_scores.json"
    app_trust_tracker = AppTrustTracker(persist_path=str(_APP_TRUST_PATH))
    logger.info(f"AppTrustTracker: using legacy JSON tracker — persist path: {_APP_TRUST_PATH}")

# ── Audit Writer ─────────────────────────────────────────────────────────────
# CompositeAuditWriter fans out to: Rich console, JSONL file, and (when
# AUDIT_SQLITE_PATH is set) the PAP's SQLite database for structured queries.
# JSONL is kept as a human-readable audit trail; SQLite is the metrics source.
_AUDIT_JSONL    = Path(__file__).parent / "logs" / "audit.jsonl"
_SQLITE_PATH    = os.getenv("AUDIT_SQLITE_PATH", "").strip()

_writers = [ConsoleAuditWriter(), JsonlAuditWriter(str(_AUDIT_JSONL))]

if _SQLITE_PATH:
    _writers.append(SqliteAuditWriter(_SQLITE_PATH))
    logger.info(f"Audit writer initialised — JSONL: {_AUDIT_JSONL} | SQLite: {_SQLITE_PATH}")
else:
    logger.info(f"Audit writer initialised — JSONL: {_AUDIT_JSONL} (SQLite sink disabled — set AUDIT_SQLITE_PATH to enable)")

audit_writer = CompositeAuditWriter(*_writers)

# ── HITL Alert Writer ─────────────────────────────────────────────────────────
# Writes PlatformAlert(category=HITL) rows to the PAP's platform_alerts table
# when a pack with hitl_trigger=true fires a non-PERMISSIVE verdict.
# Only active when AUDIT_SQLITE_PATH points to the shared PAP database.
hitl_alert_writer = HitlAlertWriter(_SQLITE_PATH) if _SQLITE_PATH else None
if hitl_alert_writer:
    logger.info(f"HITL alert writer active — target DB: {_SQLITE_PATH}")
else:
    logger.info("HITL alert writer disabled (AUDIT_SQLITE_PATH not set)")

# ── Governance Alerts Writer ──────────────────────────────────────────────────
# Emits POLICY_VIOLATION governance_alerts rows for zero-tolerance dimension
# breaches.  Loads the zero_tolerance dimension set from the PAP DB at startup.
# Only active when AUDIT_SQLITE_PATH points to the shared PAP database.
governance_alerts_writer = GovernanceAlertsWriter(_SQLITE_PATH) if _SQLITE_PATH else None
if governance_alerts_writer:
    logger.info(f"Governance alerts writer active — target DB: {_SQLITE_PATH}")
else:
    logger.info("Governance alerts writer disabled (AUDIT_SQLITE_PATH not set)")

# ── Audit Reader (Sentinel pull endpoint) ─────────────────────────────────────
# Provides GET /v1/audit/events — used by the Sentinel Agent to pull the last-N
# events for a session and compute cross-turn aggregates.
# Only active when AUDIT_SQLITE_PATH is set (same gate as SqliteAuditWriter).
audit_reader = AuditReader(_SQLITE_PATH) if _SQLITE_PATH else None
if audit_reader:
    logger.info(f"Audit reader active — GET /v1/audit/events enabled")
else:
    logger.info("Audit reader disabled (AUDIT_SQLITE_PATH not set) — GET /v1/audit/events will return 503")

# ── Sentinel Webhook Forwarder ────────────────────────────────────────────────
# Fires an async HTTP POST to the Sentinel Agent after each evaluation that
# matches a trigger condition (COERCIVE verdict OR every N turns).
# Only active when SENTINEL_WEBHOOK_URL is set.
#
# SENTINEL_WEBHOOK_URL  — full URL of the Sentinel POST /evaluate endpoint
#                         e.g. http://sentinel:3012/evaluate
# SENTINEL_TRIGGER_N    — period for the N-turn periodic scan (default 5)
SENTINEL_WEBHOOK_URL = os.getenv("SENTINEL_WEBHOOK_URL", "").strip()
SENTINEL_TRIGGER_N   = int(os.getenv("SENTINEL_TRIGGER_N", "5"))

sentinel_webhook_forwarder = (
    SentinelWebhookForwarder(SENTINEL_WEBHOOK_URL)
    if SENTINEL_WEBHOOK_URL
    else None
)
if sentinel_webhook_forwarder:
    logger.info(
        f"Sentinel webhook active — "
        f"URL: {SENTINEL_WEBHOOK_URL}  trigger_n={SENTINEL_TRIGGER_N}"
    )
else:
    logger.info("Sentinel webhook disabled (SENTINEL_WEBHOOK_URL not set)")

# ── Sentinel Sync Client ───────────────────────────────────────────────────────
# Invoked inline (synchronously) by the orchestrator when the A2A message
# accumulator reaches the configured cap.  Sentinel classifies the exchange as
# a genuine conflict/deadlock and returns NORMATIVE → orchestrator queues a
# REQUIRE_HUMAN_REVIEW directive → SDK raises HITLInterrupt.
#
# SENTINEL_SYNC_URL      Base URL of the Sentinel service (must expose /evaluate-sync).
#                        e.g. http://127.0.0.1:3012
#                        When absent, A2A conflict detection is disabled.
# A2A_HITL_THRESHOLD     Total accumulated A2A messages before sync eval fires.
#                        Default 5 = matches the mybank card-advisor showcase,
#                        which issues exactly 5 request_card_research rounds.
#                        Keep this aligned with the SDK's round count — a higher
#                        default than the round count means HITL never fires.
SENTINEL_SYNC_URL    = os.getenv("SENTINEL_SYNC_URL", "").strip()
A2A_HITL_THRESHOLD   = int(os.getenv("A2A_HITL_THRESHOLD", "5"))
# Sentinel sync timeout — the council/card arbitration LLM can take >8s, especially
# with the richer card-denial prompt. 8s caused frequent fail-open (PERMISSIVE → no HITL).
SENTINEL_SYNC_TIMEOUT_S = float(os.getenv("SENTINEL_SYNC_TIMEOUT_S", "20"))

sentinel_sync_client = (
    SentinelSyncClient(SENTINEL_SYNC_URL, timeout=SENTINEL_SYNC_TIMEOUT_S)
    if SENTINEL_SYNC_URL
    else None
)
if sentinel_sync_client:
    logger.info(
        f"Sentinel sync client active — "
        f"URL: {SENTINEL_SYNC_URL}  a2a_cap={A2A_HITL_THRESHOLD}"
    )
else:
    logger.info("Sentinel sync client disabled (SENTINEL_SYNC_URL not set) — A2A conflict detection inactive")

# ── Session Attack Detector ───────────────────────────────────────────────────
# Cross-turn sliding / salami injection and jailbreak detection.
# Analyses the last N turns as a composite to catch attacks that fragment across
# multiple turns — a known weakness of stateless per-turn guardrail evaluation.
# Always active when a session store is configured; safe to inject unconditionally.
_session_attack_detector = SessionAttackDetector()
logger.info("SessionAttackDetector initialised — multi-turn sliding attack detection active")

# ── Orchestrator ──────────────────────────────────────────────────────────────
orchestrator = PolicyOrchestrator(
    guardrail_url=GUARDRAIL_URL,
    policy_registry=policy_registry,
    deferred_dims=deferred_dims,
    http_timeout=HTTP_TIMEOUT,
    fail_mode=FAIL_MODE,
    audit_writer=audit_writer,
    session_store=session_store,
    chain_executor=chain_executor,                    # None → flat MRA fallback
    app_trust_tracker=app_trust_tracker,
    hitl_alert_writer=hitl_alert_writer,              # None when SQLite not configured
    governance_alerts_writer=governance_alerts_writer, # None when SQLite not configured
    session_attack_detector=_session_attack_detector,
    sentinel_webhook_forwarder=sentinel_webhook_forwarder,  # None when URL not set
    sentinel_trigger_n=SENTINEL_TRIGGER_N,
    sentinel_sync_client=sentinel_sync_client,             # None when URL not set
    a2a_message_cap=A2A_HITL_THRESHOLD,
)

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="RAI Trust Platform — Policy Engine",
    version="1.0.0",
    description="Verdict synthesis and policy enforcement layer (SRS §4.5)",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def _validation_err(req, exc):
    return JSONResponse(status_code=422, content={"error": str(exc)})


# Platform configuration — read-only snapshot of all Tier B runtime parameters
@app.get("/internal/config")
def get_config():
    """
    Return a read-only snapshot of the platform's runtime configuration.
    Used by PAP to display Tier B parameters alongside mock mode and fail mode.
    """
    pack_info = None
    if chain_executor is not None and _pack_loader is not None:
        pack_info = {
            "mandatory_packs": [p.id for p in _pack_loader.mandatory_packs()],
            "layer2_packs":    [p.id for p in _pack_loader.layer2_packs()],
        }
    return {
        "fail_mode":           FAIL_MODE,
        "guardrail_url":       GUARDRAIL_URL,
        "guardrail_timeout_s": HTTP_TIMEOUT,
        "session_store_type":  SESSION_STORE_TYPE,
        "session_ttl_s":       SESSION_TTL_S,
        "policy_count":        len(policy_registry.as_catalog()),
        "deferred_dims":       sorted(deferred_dims),
        "pack_engine_active":  chain_executor is not None,
        "pack_info":           pack_info,
        "db_available":        DB_AVAILABLE,
    }


# Pack inspection endpoint — returns full pack definitions for PAP visualisation
@app.get("/internal/packs")
def get_packs():
    """
    Return full serialised pack definitions from the loaded PackLoader.
    Used by the PAP Policy Packs viewer.  Read-only — no runtime state mutated.
    """
    if chain_executor is None or _pack_loader is None:
        return {"pack_engine_active": False, "packs": []}

    all_packs = [*_pack_loader.mandatory_packs(), *_pack_loader.layer2_packs()]
    return {
        "pack_engine_active": True,
        "packs": [
            {
                "id":          p.id,
                "layer":       p.layer,
                "enforcement": p.enforcement,
                "mode":        p.mode,
                "hitl_trigger": p.hitl_trigger,
                "active":      p.active,
                "version":     p.version,
                "owner":       p.owner,
                "description": p.description,
                "dimensions": [
                    {
                        "id":       d.id,
                        "profile":  d.profile,
                        "severity": d.severity,
                        "abort":    d.abort,
                        "roles":    d.roles,
                        "note":     d.note,
                        # Include base threshold band for UI display
                        "thresholds": (
                            lambda prof: {
                                "permissive": list(prof.base.permissive),
                                "normative":  list(prof.base.normative),
                                "coercive":   list(prof.base.coercive),
                            }
                            if prof else None
                        )(_pack_loader.get_profile(d.profile)),
                    }
                    for d in p.dimensions
                ],
                "context_adjustments": [
                    {
                        "signal":   a.signal,
                        "operator": a.operator,
                        "value":    a.value,
                        "step":     a.step,
                        "affects":  a.affects,
                    }
                    for a in p.context_adjustments
                ],
            }
            for p in all_packs
        ],
    }


# Internal reload endpoint — called by PAP after publishing a new policy version
@app.post("/internal/reload")
def reload_policies():
    """Hot-reload policy definitions from DB (preferred) or disk without service restart."""
    try:
        policy_registry.reload()
        if _pack_loader is not None:
            _pack_loader.reload()
        # Refresh deferred dimensions from the Guardrail Service so the PAP
        # enable/disable (deferred) toggle takes effect WITHOUT a PE restart.
        # deferred_dims is the same set object the orchestrator holds, so mutating
        # it in place updates enforcement live.
        try:
            _r = httpx.get(f"{GUARDRAIL_URL}/v1/evaluations", timeout=5.0)
            _r.raise_for_status()
            _new_deferred = {
                e["dimension"]
                for e in _r.json().get("evaluations", [])
                if e.get("status") == "deferred"
            }
            deferred_dims.clear()
            deferred_dims.update(_new_deferred)
            logger.info(f"Deferred dimensions refreshed on reload: {sorted(deferred_dims)}")
        except Exception as _de:
            logger.warning(f"Could not refresh deferred dimensions on reload: {_de}")
        logger.info("Policy definitions reloaded via /internal/reload")
        return {"status": "reloaded", "policy_count": len(policy_registry.as_catalog())}
    except Exception as e:
        logger.error(f"Hot-reload failed: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


# Session management endpoints
@app.get("/internal/sessions")
def session_stats():
    """Return session store stats — useful for PAP dashboard and debugging."""
    stats: dict = {
        "store_type": SESSION_STORE_TYPE,
        "ttl_seconds": SESSION_TTL_S,
    }
    if hasattr(session_store, "active_count"):
        stats["active_sessions"] = session_store.active_count
    return stats


@app.delete("/internal/sessions/{app_id}/{session_id}")
def delete_session(app_id: str, session_id: str):
    """Manually invalidate a session (for testing and admin use)."""
    session_store.delete(session_id, app_id)
    logger.info(f"Session manually deleted: session_id={session_id} app_id={app_id}")
    return {"deleted": True, "session_id": session_id, "app_id": app_id}


@app.post("/internal/sessions/purge")
def purge_sessions():
    """Eagerly purge all expired sessions from the store."""
    count = session_store.purge_expired(SESSION_TTL_S)
    return {"purged": count}


@app.post("/internal/a2a-reset/{session_id}")
def reset_a2a_state(session_id: str):
    """Clear the per-session A2A accumulator + one-shot HITL latch so a REPEAT
    multi-agent deliberation (e.g. re-running the card-denial demo in the same
    session) escalates to Sentinel again. The orchestrator stores A2A state under
    app_id="", so that's the key we clear."""
    session_store.delete(session_id, "")
    logger.info(f"A2A latch reset: session_id={session_id}")
    return {"reset": True, "session_id": session_id}


# Background session purge — runs every 10 minutes
import asyncio

@app.on_event("startup")
async def _start_session_purge():
    async def _purge_loop():
        while True:
            await asyncio.sleep(600)   # 10 minutes
            try:
                count = session_store.purge_expired(SESSION_TTL_S)
                if count:
                    logger.info(f"Background session purge: removed {count} expired sessions")
            except Exception as e:
                logger.error(f"Background session purge failed: {e}")
    asyncio.create_task(_purge_loop())


# ── Pack CRUD (Pack Manager) ─────────────────────────────────────────────────
# These endpoints delegate to PackManager which writes to the PAP database
# and hot-reloads PackLoader — no restart required.

@app.post("/internal/packs")
def create_pack(body: dict):
    """Create a new policy pack. Body: {id, definition}"""
    if _pack_manager is None:
        return JSONResponse(status_code=503, content={"error": "Pack engine not active"})
    pack_id    = str(body.get("id", "")).strip()
    definition = body.get("definition")
    if not pack_id or not definition:
        return JSONResponse(status_code=400, content={"error": "'id' and 'definition' are required"})
    try:
        pack = _pack_manager.create_pack(pack_id, definition)
        return {"status": "created", "pack_id": pack_id, "dimensions": len(pack.dimensions)}
    except ValueError as e:
        return JSONResponse(status_code=409, content={"error": str(e)})
    except Exception as e:
        logger.error(f"create_pack error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.put("/internal/packs/{pack_id}")
def update_pack(pack_id: str, body: dict):
    """Replace an existing pack's definition. Body: {definition}"""
    if _pack_manager is None:
        return JSONResponse(status_code=503, content={"error": "Pack engine not active"})
    definition = body.get("definition")
    if not definition:
        return JSONResponse(status_code=400, content={"error": "'definition' is required"})
    try:
        pack = _pack_manager.update_pack(pack_id, definition)
        return {"status": "updated", "pack_id": pack_id, "dimensions": len(pack.dimensions)}
    except KeyError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        logger.error(f"update_pack error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.delete("/internal/packs/{pack_id}")
def delete_pack(pack_id: str):
    """Delete a pack. Mandatory packs cannot be deleted."""
    if _pack_manager is None:
        return JSONResponse(status_code=503, content={"error": "Pack engine not active"})
    try:
        _pack_manager.delete_pack(pack_id)
        return {"status": "deleted", "pack_id": pack_id}
    except KeyError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        logger.error(f"delete_pack error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.patch("/internal/packs/{pack_id}/toggle")
def toggle_pack(pack_id: str):
    """Toggle the active flag on a pack. Mandatory packs cannot be disabled."""
    if _pack_manager is None:
        return JSONResponse(status_code=503, content={"error": "Pack engine not active"})
    try:
        new_state = _pack_manager.toggle_pack_active(pack_id)
        return {"status": "toggled", "pack_id": pack_id, "active": new_state}
    except KeyError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        logger.error(f"toggle_pack error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── App Stack endpoints ───────────────────────────────────────────────────────

def _no_app_stacks():
    return JSONResponse(status_code=503, content={"error": "App stack engine not active"})


@app.get("/internal/app-stacks")
def list_app_stacks():
    """Return all app stack definitions."""
    if _app_stack_manager is None:
        return _no_app_stacks()
    return {"app_stacks": _app_stack_manager.serialise()}


@app.get("/internal/app-stacks/{app_id}")
def get_app_stack(app_id: str):
    """Return the stack definition for one app."""
    if _app_stack_manager is None:
        return _no_app_stacks()
    stack = _app_stack_manager.get_stack(app_id)
    if stack is None:
        return JSONResponse(status_code=404, content={"error": f"No stack found for app '{app_id}'"})
    return {"app_id": app_id, "assignments": _app_stack_manager.serialise().get(app_id, {})}


@app.post("/internal/app-stacks/{app_id}/packs/{pack_id}")
def assign_pack_to_app(app_id: str, pack_id: str, body: dict = {}):
    """Assign a pack to an app (or update its active flag)."""
    if _app_stack_manager is None:
        return _no_app_stacks()
    active = bool(body.get("active", True))
    try:
        _app_stack_manager.assign_pack(app_id, pack_id, active=active)
        return {"status": "assigned", "app_id": app_id, "pack_id": pack_id, "active": active}
    except Exception as e:
        logger.error(f"assign_pack_to_app error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.delete("/internal/app-stacks/{app_id}/packs/{pack_id}")
def remove_pack_from_app(app_id: str, pack_id: str):
    """Remove a pack assignment from an app."""
    if _app_stack_manager is None:
        return _no_app_stacks()
    try:
        _app_stack_manager.remove_pack(app_id, pack_id)
        return {"status": "removed", "app_id": app_id, "pack_id": pack_id}
    except KeyError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
    except Exception as e:
        logger.error(f"remove_pack_from_app error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.patch("/internal/app-stacks/{app_id}/packs/{pack_id}/toggle")
def toggle_app_pack(app_id: str, pack_id: str):
    """Toggle the active flag for a pack assignment."""
    if _app_stack_manager is None:
        return _no_app_stacks()
    try:
        new_state = _app_stack_manager.toggle_pack(app_id, pack_id)
        return {"status": "toggled", "app_id": app_id, "pack_id": pack_id, "active": new_state}
    except KeyError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
    except Exception as e:
        logger.error(f"toggle_app_pack error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.put("/internal/app-stacks/{app_id}/packs/{pack_id}/overrides")
def set_app_pack_overrides(app_id: str, pack_id: str, body: dict):
    """Replace all dimension overrides for a pack+app combination."""
    if _app_stack_manager is None:
        return _no_app_stacks()
    overrides = body.get("overrides", [])
    try:
        _app_stack_manager.set_overrides(app_id, pack_id, overrides)
        return {"status": "overrides_set", "app_id": app_id, "pack_id": pack_id,
                "override_count": len(overrides)}
    except KeyError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
    except Exception as e:
        logger.error(f"set_app_pack_overrides error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.delete("/internal/app-stacks/{app_id}/packs/{pack_id}/overrides")
def clear_app_pack_overrides(app_id: str, pack_id: str):
    """Remove all dimension overrides for a pack+app combination."""
    if _app_stack_manager is None:
        return _no_app_stacks()
    try:
        _app_stack_manager.clear_overrides(app_id, pack_id)
        return {"status": "overrides_cleared", "app_id": app_id, "pack_id": pack_id}
    except KeyError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
    except Exception as e:
        logger.error(f"clear_app_pack_overrides error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/internal/app-stacks/{app_id}/packs/{pack_id}/propagate")
def propagate_app_pack_overrides(app_id: str, pack_id: str, body: dict):
    """Copy overrides for a pack from one app to one or more other apps."""
    if _app_stack_manager is None:
        return _no_app_stacks()
    to_apps = body.get("to_apps", [])
    if not to_apps:
        return JSONResponse(status_code=400, content={"error": "'to_apps' list is required"})
    try:
        _app_stack_manager.propagate_overrides(app_id, pack_id, to_apps)
        return {"status": "propagated", "from_app": app_id, "pack_id": pack_id,
                "to_apps": to_apps}
    except KeyError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
    except Exception as e:
        logger.error(f"propagate_overrides error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/internal/sessions/{app_id}/{session_id}")
def get_session_detail(app_id: str, session_id: str):
    """Return full session state for a specific session."""
    state = session_store.get(session_id, app_id)
    if state is None:
        return JSONResponse(status_code=404, content={"error": "Session not found or expired"})
    return {
        "session_id":              state.session_id,
        "app_id":                  state.app_id,
        "turn_count":              state.turn_count,
        "trust_score":             state.trust_score,
        "min_trust_score":         state.min_trust_score,
        "escalated":               state.escalated,
        "worst_verdict":           max(state.turn_verdicts, key=lambda v: {"PERMISSIVE": 0, "NORMATIVE": 1, "COERCIVE": 2}.get(v, 0)) if state.turn_verdicts else "PERMISSIVE",
        "high_risk_policies":      state.high_risk_policies,
        "coercive_by_interception": state.coercive_by_interception,
        "coercive_by_policy":      state.coercive_by_policy,
        "dimension_scores":        state.dimension_scores,
        "turn_verdicts":           state.turn_verdicts,
        "created_at":              state.created_at,
        "last_activity":           state.last_activity,
    }


# ── App Trust Score endpoints ─────────────────────────────────────────────────

@app.get("/internal/app-trust")
def get_app_trust():
    """
    Return per-app trust scores and recent verdict history.
    Used by PAP dashboard to display step-up/step-down status.
    """
    stats = app_trust_tracker.get_all_stats()
    return {
        "app_count": len(stats),
        "apps":      list(stats.values()),
    }


@app.delete("/internal/app-trust/{app_id}")
def reset_app_trust(app_id: str):
    """Reset an app's trust score to 1.0 (admin / testing use)."""
    try:
        app_trust_tracker.reset(app_id)
        logger.info(f"AppTrustTracker: score reset for app_id={app_id}")
        return {"reset": True, "app_id": app_id, "score": 1.0}
    except KeyError:
        return JSONResponse(status_code=404, content={"error": f"App '{app_id}' not found"})
    except AttributeError:
        # Legacy AppTrustTracker — fall back to direct state manipulation
        with app_trust_tracker._lock:
            if app_id not in app_trust_tracker._state:
                return JSONResponse(status_code=404, content={"error": f"App '{app_id}' not found"})
            app_trust_tracker._state[app_id] = {
                "score":   1.0,
                "events":  [],
                "total_evaluations": 0,
                "verdict_counts": {"COERCIVE": 0, "NORMATIVE": 0, "PERMISSIVE": 0},
            }
            app_trust_tracker._save()
        logger.info(f"AppTrustTracker (legacy): score reset for app_id={app_id}")
        return {"reset": True, "app_id": app_id, "score": 1.0}


app.include_router(
    create_router(
        orchestrator,
        policy_registry,
        GUARDRAIL_URL,
        audit_reader=audit_reader,
    )
)

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    PE_HOST = os.getenv("PE_HOST", "0.0.0.0")
    PE_PORT = int(os.getenv("PE_PORT", "3003"))
    logger.info(f"Starting Policy Engine on {PE_HOST}:{PE_PORT}")
    logger.info(f"  Guardrail Service : {GUARDRAIL_URL} (private dependency)")
    logger.info(f"  DB available      : {DB_AVAILABLE}")
    uvicorn.run(app, host=PE_HOST, port=PE_PORT, log_level="warning", timeout_graceful_shutdown=0)
