"""
RAI Trust Platform — Dashboard Service.
Port: 3004 (default — 3003 is reserved for the Policy Engine)

Provides real-time observability for governed applications.  This is
intentionally read-heavy and monitoring-focused.  Policy configuration
and provider management live in the PAP admin service (port 3002).

Port layout:
  3001  Guardrail Service
  3002  PAP (Policy Administration Point)
  3003  Policy Engine
  3004  Dashboard Service  ← this service
  3005  Trust Agents
  3012  Sentinel Agent
  3008  Coordinator Agent
  3010  Assurance / Mock proxy
  3012  Sentinel webhook receiver
  3013  Coordinator endpoint

Routes (all read-only except alert resolution):
  GET  /v1/trust-score           — composite app trust scores
  GET  /v1/trust-score/history   — trust score time-series
  GET  /v1/cogs                  — LLM cost breakdown by app
  GET  /v1/cogs/summary          — platform-wide cost summary
  GET  /v1/cogs/pricing          — model pricing reference
  GET  /v1/sre/metrics           — SRE burn-rate metrics
  GET  /v1/sre/alerts            — SRE alert list
  POST /v1/sre/alerts/{id}/ack   — acknowledge SRE alert
  POST /v1/sre/alerts/{id}/resolve — resolve SRE alert
  GET  /v1/metrics/tier2         — Tier 2 evaluation scores
  GET  /v1/alerts                — platform alerts (HITL, provider health)
  GET  /v1/alerts/{id}           — single alert
  PUT  /v1/alerts/{id}/resolve   — resolve a platform alert
  GET  /internal/alerts/stream   — SSE real-time push

Shared resources:
  - Same SQLite DB as PAP admin (PAP_DATABASE_URL env var)
  - Same core/ modules (database, models) — no data duplication
  - Separate process / port — can be deployed independently

Startup:
  python dashboard_main.py
  OR
  uvicorn dashboard_main:app --port 3004
"""
import io
import os
import sys
import logging
import logging.handlers
from pathlib import Path

from dotenv import load_dotenv

_svc_root = Path(__file__).parent
load_dotenv(_svc_root / ".env")
load_dotenv(_svc_root.parent / ".env")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# ── Logging ─────────────────────────────────────────────────────────────────
os.makedirs("logs",   exist_ok=True)

root = logging.getLogger()
root.setLevel(logging.DEBUG)
root.handlers.clear()

fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")

_stdout_utf8 = (
    io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "buffer") else sys.stdout
)
ch = logging.StreamHandler(_stdout_utf8)
ch.setLevel(logging.INFO)
ch.setFormatter(fmt)
root.addHandler(ch)

fh = logging.handlers.RotatingFileHandler(
    "logs/dashboard.log", maxBytes=10 * 1024 * 1024, backupCount=5
)
fh.setLevel(logging.DEBUG)
fh.setFormatter(fmt)
root.addHandler(fh)

logger = logging.getLogger(__name__)

# ── DB init (same DB as PAP — read-only posture, but writes for alert resolution) ──
from core.database import init_db
init_db()
logger.info("Dashboard: DB connection verified")

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="RAI Trust Platform — Dashboard",
    version="0.2.0",
    description=(
        "Real-time observability dashboard for governed AI applications. "
        "Provides trust scores, cost analytics, SRE metrics, and alert management."
    ),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def _validation_err(req, exc):
    return JSONResponse(status_code=422, content={"error": str(exc)})


# ── Dashboard routers ─────────────────────────────────────────────────────────
from api.trust_score import router as trust_score_router
from api.cogs        import router as cogs_router
from api.sre_metrics import router as sre_router
from api.tier2       import router as tier2_router
from api.alerts      import router as alerts_router

app.include_router(trust_score_router)
app.include_router(cogs_router)
app.include_router(sre_router)
app.include_router(tier2_router)
app.include_router(alerts_router)

# ── Assurance Service (Tier 2 quality metrics via SPI) ────────────────────────
# Same env-driven provider selection as PAP admin (ASSURANCE_PROVIDER in pap/.env).
# Registering here means port-3004 callers get /v1/assurance/metrics without
# going through the PAP admin process.
from api.assurance.service       import router as assurance_router, registry as assurance_registry
from api.assurance.mock_provider import MockAssuranceProvider

_assurance_provider_name = os.getenv("ASSURANCE_PROVIDER", "mock").lower()

if _assurance_provider_name == "arize":
    from api.assurance.arize_provider import ArizePhoenixProvider
    _assurance_provider = ArizePhoenixProvider()
else:
    _assurance_provider = MockAssuranceProvider()

assurance_registry.register(_assurance_provider)
logger.info(
    f"[Assurance] {_assurance_provider.name()} registered on Dashboard "
    f"(ASSURANCE_PROVIDER={_assurance_provider_name!r})"
)
app.include_router(assurance_router)


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/v1/health", include_in_schema=True, tags=["health"])
def health():
    return {
        "service": "dashboard",
        "status":  "ok",
        "version": "0.2.0",
    }


# ── Static UI ─────────────────────────────────────────────────────────────────
# Serve from pap/dashboard_ui/ (create this directory when the Dashboard SPA
# is built).  Falls back to a minimal placeholder response.
_UI_DIR = Path(__file__).parent / "dashboard_ui"
if _UI_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_UI_DIR)), name="static")

    @app.get("/ui", include_in_schema=False)
    @app.get("/ui/", include_in_schema=False)
    def serve_ui():
        return FileResponse(str(_UI_DIR / "index.html"))

    @app.get("/", include_in_schema=False)
    def root_redirect():
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/ui")
else:
    @app.get("/", include_in_schema=False)
    def root_info():
        return {
            "service": "RAI Trust Platform — Dashboard",
            "version": "0.2.0",
            "note":    (
                "Dashboard UI not yet built. "
                "Create pap/dashboard_ui/index.html to serve the SPA. "
                "API is fully functional — see /docs."
            ),
            "api":     "http://localhost:3004/docs",
            "pap":     "http://localhost:3002/ui",
        }


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    HOST = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    PORT = int(os.getenv("DASHBOARD_PORT", "3004"))
    logger.info(f"Starting Dashboard on {HOST}:{PORT}")
    logger.info(f"  API docs : http://localhost:{PORT}/docs")
    logger.info(f"  PAP admin: http://localhost:3002/ui")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
