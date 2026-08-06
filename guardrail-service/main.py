"""
Guardrail Service — entry point.
Port: 3001 (internal, called only by the Policy Engine)

Startup sequence:
  1. Load evaluation taxonomy (DB-only — no YAML fallback)
  2. Load provider config  (DB-only — no YAML fallback)
  3. Register providers (Mock or real Azure/AWS depending on MOCK_MODE)
  4. Start FastAPI with /v1/trust/evaluate, /v1/providers, /v1/evaluations, /v1/health

Error philosophy: this service never calls sys.exit on config failure.
If the DB is unreachable, the process starts in *degraded mode*:
  GET /v1/health          → 503  (with structured error detail)
  POST /v1/trust/evaluate → 503  (with structured error detail)
This keeps the service observable and restartable without crashing uvicorn.
"""
import io
import os
import re
import sys
import logging
import logging.handlers

from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI

# Layered .env loading:
#   1. Service-level  guardrail-service/.env  — provider credentials, GS-specific config
#   2. Root           rai-trust-platform/.env  — shared platform defaults (fallback only)
# Service-level values win; root fills any gaps not set at service level.
_svc_root = Path(__file__).parent
load_dotenv(_svc_root / ".env")                      # service-level (wins)
load_dotenv(_svc_root.parent / ".env")               # root fallback
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from core.database              import GS_DB_AVAILABLE
from core.evaluation_registry   import EvaluationRegistry
from core.provider_registry     import ProviderRegistry
from providers.mock_provider              import MockProvider
from providers.azure_provider             import AzureContentSafetyProvider
from providers.aws_provider               import AWSBedrockProvider
from providers.local_policy_classifier    import LocalPolicyClassifierProvider
from providers.embedding_filter           import EmbeddingFilterProvider
from providers.transfer_security_provider import TransferSecurityProvider
from api.routes                import create_router

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
    "logs/guardrail-service.log", maxBytes=10 * 1024 * 1024, backupCount=5
)
fh.setLevel(logging.DEBUG)
fh.setFormatter(fmt)
root.addHandler(fh)

logger = logging.getLogger(__name__)

logger.info(f"Guardrail Service: PAP DB available = {GS_DB_AVAILABLE}")

# ── Config helpers ───────────────────────────────────────────────────────────
def _resolve_env(text: str) -> str:
    """Expand ${VAR} references using current environment variables."""
    return re.sub(r"\$\{(\w+)\}", lambda m: os.getenv(m.group(1), m.group(0)), text)


# Accumulated startup errors — passed to routes so /v1/health can surface them.
_startup_errors: list[str] = []


def _load_providers_cfg() -> dict:
    """
    Load providers configuration from the PAP database (DB-only, no YAML fallback).

    Returns the parsed dict with a 'providers' key.
    Returns {"providers": []} and appends to _startup_errors on failure so the
    service continues in degraded mode rather than calling sys.exit.
    """
    if GS_DB_AVAILABLE:
        try:
            from core.database import query_service_config
            cfg = query_service_config("gs", "providers")
            if cfg:
                logger.info("Providers config loaded from PAP DB")
                import json as _json
                resolved = _resolve_env(_json.dumps(cfg))
                return _json.loads(resolved)
            else:
                msg = (
                    "gs:providers config not found in PAP DB. "
                    "Run pap/bootstrap.py to seed the database."
                )
                logger.critical(f"[providers] {msg}")
                _startup_errors.append(msg)
                return {"providers": []}
        except Exception as exc:
            msg = f"Failed to load providers config from DB: {exc}"
            logger.critical(f"[providers] {msg}", exc_info=True)
            _startup_errors.append(msg)
            return {"providers": []}
    else:
        msg = (
            "PAP database is not reachable — cannot load providers config. "
            "Set PAP_DB_PATH and run pap/bootstrap.py."
        )
        logger.critical(f"[providers] {msg}")
        _startup_errors.append(msg)
        return {"providers": []}


# ── Registries ───────────────────────────────────────────────────────────────
# EvaluationRegistry never raises — it enters degraded mode and sets
# _startup_error internally.  Check eval_registry.is_healthy() at runtime.
eval_registry = EvaluationRegistry()
if not eval_registry.is_healthy():
    _startup_errors.append(eval_registry._startup_error or "Evaluation registry failed to load")

provider_registry = ProviderRegistry()
MOCK_MODE = os.getenv("MOCK_MODE", "false").lower() in ("true", "1", "yes")

# ── Always register MockProvider ─────────────────────────────────────────────
# MockProvider is registered in all modes — it is the active provider when
# MOCK_MODE=true (set via environment variable at startup, not at runtime).
_mock_provider_inst = MockProvider()
provider_registry.register(_mock_provider_inst)

# ── Always register LocalPolicyClassifierProvider ────────────────────────────
# In-process, no credentials needed — registered in all modes.
_local_policy_inst = LocalPolicyClassifierProvider("config/policy_signals.yaml")
provider_registry.register(_local_policy_inst)

# ── Always register EmbeddingFilterProvider ──────────────────────────────────
# Lazy-loads all-MiniLM-L6-v2 on first call; fails open if model unavailable.
_embedding_filter_inst = EmbeddingFilterProvider("config/embedding_filter.yaml")
provider_registry.register(_embedding_filter_inst)

# ── Always register TransferSecurityProvider ─────────────────────────────────
# In-process, no credentials needed — handles prompt_injection and jailbreak
# dimensions with transfer-context amplification.  Never overridden in MOCK mode.
_transfer_security_inst = TransferSecurityProvider()
provider_registry.register(_transfer_security_inst)
logger.info("TransferSecurityProvider registered — in-process injection/jailbreak detection active")

# Count of always-present in-process providers (mock + 3 local) — used to
# detect when no real external providers were registered in non-MOCK mode.
_LOCAL_PROVIDER_COUNT = len(provider_registry.all_providers())

if MOCK_MODE:
    logger.warning("=" * 60)
    logger.warning("  MOCK MODE ENABLED — no real provider API calls")
    logger.warning(f"  MOCK_SCENARIO = {os.getenv('MOCK_SCENARIO', '(content-aware)')}")
    logger.warning("=" * 60)
    # Point all active dimensions at mock_provider, except local in-process providers
    # which don't make external calls and don't need mocking.
    _LOCAL_PROVIDERS = {"local_policy_classifier", "embedding_filter", "transfer_security_provider"}
    for dim_name, spec in eval_registry._dimensions.items():
        if spec.provider not in _LOCAL_PROVIDERS:
            spec.provider = "mock_provider"
else:
    cfg = _load_providers_cfg()

    for p in cfg.get("providers", []):
        if not p.get("active", True):
            continue
        try:
            ptype = p["type"]
            if ptype == "azure":
                inst = AzureContentSafetyProvider(
                    endpoint=p["endpoint"], api_key=p["api_key"]
                )
            elif ptype == "aws":
                inst = AWSBedrockProvider(
                    guardrail_arn=p["guardrail_arn"],
                    guardrail_version=p.get("guardrail_version", "DRAFT"),
                    region_name=p["region"],
                    aws_access_key_id=p["api_key"],
                    aws_secret_access_key=p["aws_secret_key"],
                )
            else:
                logger.warning(f"Unknown provider type '{ptype}' — skipped")
                continue
            provider_registry.register(inst)
        except Exception as e:
            logger.error(f"Failed to register provider '{p.get('name')}': {e}")

if len(provider_registry.all_providers()) <= _LOCAL_PROVIDER_COUNT and not MOCK_MODE:
    # Only in-process providers registered and real mode was requested
    msg = (
        "No real providers registered and MOCK_MODE=false. "
        "Add provider credentials to pap DB (gs:providers config) or set MOCK_MODE=true."
    )
    logger.critical(msg)
    _startup_errors.append(msg)

# ── Capability matrix ────────────────────────────────────────────────────────
logger.info("Provider capability matrix:")
for dim in eval_registry.active_dimensions():
    spec = eval_registry.get_spec(dim)
    logger.info(f"  {dim:<25} provider={spec.provider:<25} roles={sorted(spec.roles)}")

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="RAI Trust Platform — Guardrail Service",
    version="1.0.0",
    description="Provider-agnostic content evaluation engine (SRS §4.6)",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def _validation_err(req, exc):
    return JSONResponse(status_code=422, content={"error": str(exc)})


app.include_router(create_router(provider_registry, eval_registry, _startup_errors))

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    GS_HOST = os.getenv("GS_HOST", "127.0.0.1")
    GS_PORT = int(os.getenv("GS_PORT", "3001"))
    logger.info(f"Starting Guardrail Service on {GS_HOST}:{GS_PORT}")
    if GS_HOST == "127.0.0.1":
        logger.info("  Binding to localhost only — private, not reachable from outside this machine")
    else:
        logger.info(f"  Binding to {GS_HOST} — reachable on all interfaces")
    uvicorn.run(app, host=GS_HOST, port=GS_PORT, log_level="warning", timeout_graceful_shutdown=0)
