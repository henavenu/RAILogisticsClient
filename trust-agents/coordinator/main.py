"""
Coordinator Agent — FastAPI service, port 3006.

Role in the Trust Agent system
───────────────────────────────
  Sentinel   (port 3005) — DETECTS cross-turn patterns → ALERT / WATCH / CLEAR
  Coordinator (port 3006) — RECOMMENDS actions based on Sentinel verdicts

The Coordinator is called:
  • On-demand by the PAP dashboard ("Coordinate" button for a session)
  • Automatically by Sentinel when its verdict == ALERT (fire-and-forget POST)
  • Never by the SDK or application — completely invisible to the governed app

Endpoints
─────────
  POST /coordinate              — trigger coordination for a session (async, 202 ACK)
  GET  /reports/{session_id}    — latest recommendation report for a session
  GET  /reports                 — all stored reports (newest first)
  GET  /health                  — liveness check
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import uvicorn
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ── Path & env ────────────────────────────────────────────────────────────────
_svc_root = Path(__file__).resolve().parent
sys.path.insert(0, str(_svc_root))

load_dotenv(_svc_root / ".env")                    # coordinator/.env  (wins)
load_dotenv(_svc_root.parent / ".env")             # trust-agents/.env  (LLM_BASE_URL, LLM_MODEL)
load_dotenv(_svc_root.parent.parent / ".env")      # repo root fallback

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ── Required-config validation ──────────────────────────────────────────────
# Peer-service URLs have NO silent fallback — a missing one aborts startup with a
# clear error instead of defaulting to a dead localhost port. (The only legitimate
# fallback in this platform is the LLM provider chain.)
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

# ── Configuration ──────────────────────────────────────────────────────────────
# REQUIRED — the Coordinator pulls findings from Sentinel and writes alerts to PAP.
SENTINEL_URL  = _require_env("COORDINATOR_SENTINEL_URL", "Coordinator reads findings from the Sentinel")
PAP_URL       = _require_env("COORDINATOR_PAP_URL",      "Coordinator writes governance alerts to PAP")
HOST          = os.getenv("COORDINATOR_HOST",         "0.0.0.0")   # bind default — OK
PORT          = int(os.getenv("COORDINATOR_PORT",     "3013"))     # canonical bind port
_abort_if_missing_env()
POLICY_PATH   = Path(os.getenv("COORDINATOR_POLICY_PATH", str(_svc_root / "policy.yaml")))
HTTP_TIMEOUT  = float(os.getenv("COORDINATOR_HTTP_TIMEOUT", "5"))

# ── Load policy ───────────────────────────────────────────────────────────────
try:
    with open(POLICY_PATH, "r", encoding="utf-8") as _fh:
        _policy = yaml.safe_load(_fh)
    logger.info(f"Policy loaded: {POLICY_PATH}")
except FileNotFoundError:
    logger.critical(f"Policy file not found: {POLICY_PATH}")
    sys.exit(1)
except Exception as e:
    logger.critical(f"Failed to load policy: {e}")
    sys.exit(1)

# ── Core components ───────────────────────────────────────────────────────────
from store       import ReportStore
from coordinator import CoordinatorEngine
from schemas     import CoordinatorRequest, ReportResponse

_report_store = ReportStore()
_engine       = CoordinatorEngine(
    policy       = _policy,
    sentinel_url = SENTINEL_URL,
    store        = _report_store,
    http_timeout = HTTP_TIMEOUT,
    pap_url      = PAP_URL,
)

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title       = "RAI Trust Platform — Coordinator Agent",
    version     = "0.1.0",
    description = (
        "Recommendation layer. Reads Sentinel verdicts and produces "
        "actionable guidance for operators and compliance teams."
    ),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────────────
#  POST /coordinate   — trigger coordination (202 ACK + background task)
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/coordinate", status_code=202)
async def coordinate(request: CoordinatorRequest):
    """
    Trigger a coordination cycle for a session.

    Returns HTTP 202 immediately — the recommendation pipeline runs as a
    background asyncio task.  Poll GET /reports/{session_id} for results.

    The Sentinel Agent calls this automatically when its verdict == ALERT.
    The PAP dashboard can call it on-demand for any session.
    """
    logger.info(
        f"[/coordinate] Request — "
        f"session={request.session_id}  "
        f"app={request.app_id}  "
        f"sentinel={request.sentinel_verdict or '(to fetch)'}"
    )
    asyncio.create_task(_run_coordination(request))
    return {
        "status":     "accepted",
        "session_id": request.session_id,
    }


async def _run_coordination(request: CoordinatorRequest) -> None:
    """Background task: run the full coordination pipeline."""
    try:
        await _engine.coordinate(request)
    except Exception as exc:
        logger.exception(
            f"[Coordinator] Unhandled error for session={request.session_id}: {exc}"
        )


# ─────────────────────────────────────────────────────────────────────────────
#  GET /reports/{session_id}   — latest report for one session
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/reports/{session_id}", response_model=ReportResponse)
def get_report(session_id: str):
    """
    Return the latest CoordinatorReport for a session.

    Returns 200 with ``found=false`` when the session has not yet been
    coordinated — the caller should not treat this as an error.
    """
    report = _report_store.get(session_id)
    return ReportResponse(
        session_id = session_id,
        found      = report is not None,
        report     = report,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  GET /reports   — all stored reports (newest first)
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/reports")
def list_reports():
    """All stored CoordinatorReports, newest first."""
    reports = _report_store.all()
    return {
        "total":   len(reports),
        "reports": [r.model_dump() for r in reports],
    }


# ─────────────────────────────────────────────────────────────────────────────
#  GET /health
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    """Liveness check."""
    return {
        "status":          "healthy",
        "sentinel_url":    SENTINEL_URL,
        "reports_stored":  len(_report_store),
        "llm_active":      _engine._llm_call is not None,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("  RAI Trust Platform — Coordinator Agent")
    logger.info(f"  Port         : {PORT}")
    logger.info(f"  Sentinel URL : {SENTINEL_URL}")
    logger.info(f"  Policy       : {POLICY_PATH}")
    logger.info("=" * 60)

    uvicorn.run(
        "main:app",
        host      = HOST,
        port      = PORT,
        reload    = False,
        log_level = "info",
        timeout_graceful_shutdown = 0,
    )
