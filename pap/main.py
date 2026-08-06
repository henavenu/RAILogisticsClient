"""
PAP — Policy Administration Point.
Port: 3002

Phase 2 capabilities:
  - SQLite-backed policy store (CRUD via REST)
  - Web UI for policy management (4 tabs: Policies, Evaluations, App Config, Audit)
  - Hot-reload trigger to Policy Engine on policy publish
  - App-level trust tier configuration
  - Audit trail for all policy changes

Startup sequence:
  1. Initialise SQLite DB (auto-create tables)
  2. Seed policy store from policy-engine/config/policies.yaml (if empty)
  3. Mount static UI files
  4. Start FastAPI with all v1 routes
"""
import io
import os
import sys
import logging
import logging.handlers
from pathlib import Path

from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI

# Layered .env loading:
#   1. Service-level  pap/.env  — PAP-specific config, DB URL, secret key
#   2. Root           rai-trust-platform/.env  — shared platform defaults (fallback only)
# Service-level values win; root fills any gaps not set at service level.
_svc_root = Path(__file__).parent
load_dotenv(_svc_root / ".env")                      # service-level (wins)
load_dotenv(_svc_root.parent / ".env")               # root fallback
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# ── Logging ─────────────────────────────────────────────────────────────────
os.makedirs("logs",   exist_ok=True)
os.makedirs("config", exist_ok=True)

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
    "logs/pap.log", maxBytes=10 * 1024 * 1024, backupCount=5
)
fh.setLevel(logging.DEBUG)
fh.setFormatter(fmt)
root.addHandler(fh)

logger = logging.getLogger(__name__)

# ── Database init ─────────────────────────────────────────────────────────────
from core.database    import init_db, SessionLocal
from core.seed        import seed_all
from core.seed_pricing import seed_pricing

init_db()
logger.info("PAP database initialised (Alembic upgrade head + create_all safety net)")
logger.info(
    "Post-pull reminder: if policy_messages.yaml was updated, run: "
    "python policy-engine/config/apply_messages.py"
)

# Seed all config tables from YAML sources on first run (idempotent)
with SessionLocal() as _seed_db:
    _seed_results = seed_all(_seed_db)
    _inserted = sum(_seed_results.values())
    if _inserted:
        logger.info(f"Seed complete — {_inserted} rows inserted: {_seed_results}")
    else:
        logger.info("All config tables already populated — seed skipped")

    # Seed model pricing table (idempotent — existing rows are never overwritten)
    _pricing_inserted = seed_pricing(_seed_db)
    if _pricing_inserted:
        logger.info(f"Model pricing seed — {_pricing_inserted} new rows inserted into config_model_pricing")
    else:
        logger.info("Model pricing already populated — pricing seed skipped")

# ── Startup config summary banner ─────────────────────────────────────────────
# Logged on EVERY boot (not just first-run insert counts) so the container logs
# always show what actually loaded — answering "are policies loaded, which
# providers are assigned, did it fail" without guessing from a green dashboard.
# Same data is exposed live at GET /v1/diag/config.
try:
    from core.models import Policy as _Policy, EvaluationDimension as _Dim, Provider as _Prov
    with SessionLocal() as _diag_db:
        _pol_active = _diag_db.query(_Policy).filter(_Policy.active == True).count()  # noqa: E712
        _pol_total  = _diag_db.query(_Policy).count()
        _dims       = _diag_db.query(_Dim).all()
        _dim_active = [d for d in _dims if d.status == "active"]
        _provs      = _diag_db.query(_Prov).all()
        _prov_active = [p for p in _provs if getattr(p, "active", True)]
    logger.info("=" * 70)
    logger.info(
        "[PAP CONFIG] policies: %d active / %d total | dimensions: %d active / %d total "
        "| providers: %d active / %d total",
        _pol_active, _pol_total, len(_dim_active), len(_dims),
        len(_prov_active), len(_provs),
    )
    _map = ", ".join(f"{d.dimension_id}->{d.provider}" for d in _dim_active[:40])
    logger.info("[PAP CONFIG] active dimension→provider: %s", _map or "(none)")
    logger.info(
        "[PAP CONFIG] active providers: %s",
        ", ".join(p.provider_id for p in _prov_active) or "(none)",
    )
    if _pol_active == 0:
        logger.critical(
            "[PAP CONFIG] WARNING: 0 ACTIVE policies — every evaluation will be PERMISSIVE "
            "(seeding did not run or DB is empty)."
        )
    if len(_prov_active) == 0:
        logger.critical("[PAP CONFIG] WARNING: 0 ACTIVE providers in the registry.")
    logger.info("[PAP CONFIG] Full detail at GET /v1/diag/config")
    logger.info("=" * 70)
except Exception as _diag_exc:
    logger.error("[PAP CONFIG] startup config summary failed: %s", _diag_exc)

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="RAI Trust Platform — Policy Administration Point",
    version="0.2.0",
    description="Control Plane for policy management, publishing, and audit (SRS §4.7)",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def _validation_err(req, exc):
    return JSONResponse(status_code=422, content={"error": str(exc)})


# ── API routes ────────────────────────────────────────────────────────────────
from api.routes    import create_router
from api.analytics import router as analytics_router
app.include_router(create_router())
app.include_router(analytics_router)

# ── Dashboard-tier API routers ─────────────────────────────────────────────────
# These routes are also served by dashboard_main.py (port 3004), which is the
# authoritative Dashboard service.  They remain here for backward compatibility
# while the UI is migrated to call the dedicated Dashboard backend.
# TODO: remove from PAP once dashboard_ui/ is deployed and calling port 3004.
from api.cogs               import router as cogs_router
from api.trust_score        import router as trust_score_router
from api.sre_metrics        import router as sre_router
from api.tier2              import router as tier2_router
from api.alerts             import router as alerts_router
from api.governance_alerts  import router as governance_alerts_router

app.include_router(cogs_router)
app.include_router(trust_score_router)
app.include_router(sre_router)
app.include_router(tier2_router)
app.include_router(alerts_router)
app.include_router(governance_alerts_router)

# ── Assurance Service (SRS §4.7) ──────────────────────────────────────────────
# Observability & quality metrics via AssuranceProvider SPI.
#
# Provider selection is controlled by ASSURANCE_PROVIDER in pap/.env:
#   mock  (default) — MockAssuranceProvider → mock proxy at ASSURANCE_URL (3010)
#   arize           — ArizePhoenixProvider  → Phase 1: seed values
#                                             Phase 3: live Arize Phoenix
#
# Single env flip to switch:  ASSURANCE_PROVIDER=arize  in pap/.env
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
    f"[Assurance] {_assurance_provider.name()} registered — "
    f"{len(assurance_registry.all_metrics())} T2 metrics available "
    f"(ASSURANCE_PROVIDER={_assurance_provider_name!r}, "
    f"ASSURANCE_URL={os.getenv('ASSURANCE_URL', 'http://127.0.0.1:3010')})"
)
app.include_router(assurance_router)


# ── Rolling retention for high-growth audit + metrics tables (opt-in) ──────────
# Keeps the eval-trail and metric snapshots to a configured rolling window so the
# DB stays bounded under sustained traffic. OFF unless PAP_RETENTION_DAYS (or the
# per-category overrides) is set. Started here so a running event loop exists.
@app.on_event("startup")
async def _start_retention() -> None:
    try:
        from core.retention import start_retention
        start_retention()
    except Exception as _exc:
        logger.warning("[retention] failed to start: %s", _exc)

# ── UI runtime config (externalises hardcoded URLs from dashboard.html) ───────
def _ui_url(browser_var: str, server_var: str, default: str) -> str:
    """
    Resolve a BROWSER-facing service URL for the dashboard JS.

    These URLs are dereferenced by the user's browser, not the server — so behind a
    reverse proxy (single-container / Cloud Run) the agent services are NOT reachable
    on their own loopback ports. In that topology the entrypoint sets the UI_* vars
    to same-origin proxied paths (e.g. UI_SENTINEL_URL=/agents/sentinel), which nginx
    routes to the loopback service. In local dev the UI_* vars are unset and we fall
    back to the server-side URL (localhost:<port>), which the browser CAN reach
    directly. Never return a server-side loopback URL to the browser behind a proxy.
    """
    return os.getenv(browser_var, "").strip() or os.getenv(server_var, default)


@app.get("/v1/ui-config", include_in_schema=False)
def ui_config():
    """
    Returns BROWSER-reachable service URLs so the dashboard is environment-agnostic.
    Prefers UI_* (browser-facing, proxy-aware); falls back to the server-side URL /
    hardcoded default for local dev. See _ui_url() and docker/entrypoint.sh.
    """
    return {
        "assurance_url":   _ui_url("UI_ASSURANCE_URL",   "ASSURANCE_URL",    "http://127.0.0.1:3010"),
        "sentinel_url":    _ui_url("UI_SENTINEL_URL",    "SENTINEL_URL",     "http://127.0.0.1:3012"),
        "coordinator_url": _ui_url("UI_COORDINATOR_URL", "COORDINATOR_URL",  "http://127.0.0.1:3013"),
        "trust_url":       _ui_url("UI_TRUST_URL",       "TRUST_AGENTS_URL", "http://127.0.0.1:3005"),
    }


# ── Static UI ─────────────────────────────────────────────────────────────────
_UI_DIR = Path(__file__).parent / "ui"
if _UI_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_UI_DIR)), name="static")

    _NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}

    @app.get("/ui", include_in_schema=False)
    @app.get("/ui/", include_in_schema=False)
    def serve_ui():
        return FileResponse(str(_UI_DIR / "index.html"), headers=_NO_CACHE)

    @app.get("/ui/dashboard", include_in_schema=False)
    def serve_dashboard():
        return FileResponse(str(_UI_DIR / "dashboard.html"), headers=_NO_CACHE)

    # Self-contained Agent Activity widget — embedded in the dashboard tab via
    # <iframe> and also openable directly here for independent testing.
    @app.get("/ui/agent-activity", include_in_schema=False)
    def serve_agent_activity():
        return FileResponse(str(_UI_DIR / "agent_activity.html"), headers=_NO_CACHE)

    @app.get("/", include_in_schema=False)
    def root_redirect():
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/ui")
else:
    @app.get("/", include_in_schema=False)
    def root_info():
        return {
            "service": "RAI Trust Platform — Policy Administration Point",
            "version": "0.2.0",
            "ui":      "UI directory not found. Create pap/ui/index.html.",
            "api":     "http://localhost:3002/docs",
        }

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    PAP_HOST = os.getenv("PAP_HOST", "0.0.0.0")
    PAP_PORT = int(os.getenv("PAP_PORT", "3002"))
    _policies_yaml = Path(__file__).parent.parent / "policy-engine" / "config" / "policies.yaml"
    logger.info(f"Starting PAP on {PAP_HOST}:{PAP_PORT}")
    logger.info(f"  Web UI      : http://localhost:{PAP_PORT}/ui")
    logger.info(f"  API docs    : http://localhost:{PAP_PORT}/docs")
    logger.info(f"  Policy YAML : {_policies_yaml}")
    uvicorn.run(app, host=PAP_HOST, port=PAP_PORT, log_level="warning", timeout_graceful_shutdown=0)
