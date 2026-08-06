"""
Trust Agents Service
Port: 3005

Two scheduled agents:
  - EU AI Act Compliance Monitor  (fires every SNAPSHOT_INTERVAL_MINUTES)
  - App / Agent Trust Tracker     (fires every SNAPSHOT_INTERVAL_MINUTES)

Both agents:
  1. Collect T1/T2/session snapshots from PAP
  2. Detect compliance/trust drift (deterministic Python)
  3. Call local LLM (Ollama) with chain-of-thought prompt
  4. Store finding + reasoning trace in ta.db

REST API (port 3005):
  GET  /v1/trust/findings              latest finding per agent
  GET  /v1/trust/drift                 active drift signals
  GET  /v1/trust/history               historical findings
  GET  /v1/trust/status                agent health
  POST /v1/trust/run/{agent_type}      trigger immediate run (sync)
  GET  /v1/trust/stream/{agent_type}   trigger run + stream SSE reasoning
  GET  /v1/health                      service health
"""
import asyncio
import logging
import logging.handlers
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

_svc_root = Path(__file__).parent
load_dotenv(_svc_root / ".env")
load_dotenv(_svc_root.parent / ".env")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from core.database import init_db
from core import compliance_agent, tracker_agent
from api.findings import router as findings_router
from api.run      import router as run_router

# ── Logging ───────────────────────────────────────────────────────────────────
_LOG_DIR = _svc_root / "logs"
_LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            _LOG_DIR / "trust-agents.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
        ),
    ],
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
_DB_PATH           = os.getenv("TA_DB_PATH", str(_svc_root / "ta.db"))
_INTERVAL_MINUTES  = int(os.getenv("SNAPSHOT_INTERVAL_MINUTES", "2"))
_INTERVAL_SECONDS  = _INTERVAL_MINUTES * 60


# ── Background scheduler ──────────────────────────────────────────────────────

async def _scheduler_loop():
    """Fire both agents every SNAPSHOT_INTERVAL_MINUTES. Errors never crash the loop."""
    logger.info(f"[Scheduler] Starting — interval={_INTERVAL_MINUTES}min")
    while True:
        await asyncio.sleep(_INTERVAL_SECONDS)
        for name, module in [("compliance", compliance_agent), ("tracker", tracker_agent)]:
            try:
                result = await module.run_sync()
                logger.info(
                    f"[Scheduler] {name} -> "
                    f"status={result.get('overall_status')} "
                    f"severity={result.get('severity')}"
                )
            except Exception as exc:
                logger.error(f"[Scheduler] {name} agent error: {exc}")


# ── App lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info(f"[TrustAgents] Initialising DB at {_DB_PATH}")
    init_db(_DB_PATH)
    os.environ["TA_DB_PATH"] = _DB_PATH   # propagate to agent modules

    scheduler_task = asyncio.create_task(_scheduler_loop())
    logger.info("[TrustAgents] Service ready")
    yield

    # Shutdown
    scheduler_task.cancel()
    try:
        await scheduler_task
    except asyncio.CancelledError:
        pass
    logger.info("[TrustAgents] Shutdown complete")


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Trust Agents",
    description="EU AI Act Compliance Monitor + App/Agent Trust Tracker",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(findings_router)
app.include_router(run_router)


@app.get("/v1/health", tags=["Health"])
async def health():
    from core import llm_client
    llm_ok = await llm_client.is_available()
    return {
        "status":    "ok",
        "service":   "trust-agents",
        "port":      3005,
        "llm_url":   os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
        "llm_model": os.getenv("LLM_MODEL", "mistral:7b-instruct-q4_K_M"),
        "llm_ok":    llm_ok,
        "db_path":   _DB_PATH,
        "schedule":  f"every {_INTERVAL_MINUTES} min",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=3005,
        reload=False,
        # Release the TCP socket immediately on shutdown so the port is free
        # for the next start without waiting for OS TIME_WAIT to expire.
        timeout_graceful_shutdown=0,
    )
