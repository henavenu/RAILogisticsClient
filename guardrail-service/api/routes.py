"""
Guardrail Service API routes.

Endpoints (SRS §4.6.5):
  POST /v1/trust/evaluate       — core evaluation endpoint
  GET  /v1/providers            — current provider registry state
  GET  /v1/evaluations          — full evaluation taxonomy with active/deferred status
  GET  /v1/health               — liveness/readiness, verifies provider connectivity

Internal (dev / ops only):
  POST /internal/mock-signals/reload   — hot-reload keyword signals (dev only)
  GET  /internal/corporate-policy/signals  — read corporate policy signal list
  PUT  /internal/corporate-policy/signals  — update corporate policy signal list
"""
import logging
import yaml
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from schemas.request  import TrustEvaluateRequest
from schemas.response import TrustEvaluateResponse, ProviderResult, EvaluationResult
from core.provider_registry   import ProviderRegistry
from core.evaluation_registry import EvaluationRegistry

logger = logging.getLogger(__name__)


def create_router(
    provider_registry: ProviderRegistry,
    eval_registry:     EvaluationRegistry,
    startup_errors:    list | None = None,
) -> APIRouter:

    router = APIRouter()
    _startup_errors = startup_errors or []

    # ------------------------------------------------------------------ #
    #  POST /v1/trust/evaluate                                            #
    # ------------------------------------------------------------------ #
    @router.post("/v1/trust/evaluate", response_model=TrustEvaluateResponse)
    def evaluate(request: TrustEvaluateRequest):
        """
        Route content to configured providers and return normalised evaluation results.
        Only ACTIVE dimensions are dispatched; deferred dimensions are silently excluded.
        Returns 503 when the service is in degraded mode (DB unavailable at startup).
        """
        if not eval_registry.is_healthy():
            return JSONResponse(
                status_code=503,
                content={
                    "error":       "service_config_unavailable",
                    "message":     "Guardrail Service is in degraded mode — evaluation taxonomy could not be loaded.",
                    "detail":      eval_registry._startup_error,
                    "remediation": "Run pap/bootstrap.py to seed the PAP database, then restart guardrail-service.",
                },
            )

        role = request.content_role.value

        # Resolve provider → [dimensions] mapping (active dims only)
        resolved = eval_registry.resolve_providers(
            request.evaluations, role, include_deferred=False
        )

        if not resolved:
            logger.warning(
                f"[{request.request_id}] No active dimensions resolved "
                f"for role='{role}', evaluations={request.evaluations}"
            )
            return TrustEvaluateResponse(
                request_id=request.request_id,
                provider_results=[],
                errors=[{"error": "No active dimensions applicable for this role/evaluation set"}],
            )

        provider_results = []
        errors = []

        for provider_name, dims in resolved.items():
            provider = provider_registry.get(provider_name)
            if not provider:
                # Provider is assigned to dimensions in the registry but was never
                # registered in the GS.  Emit explicit fail-open results so these
                # dimensions always appear in the response with a clear metadata note
                # rather than being silently dropped.
                logger.error(
                    f"Provider '{provider_name}' not registered \u2014 "
                    f"returning fail-open PASS for dims: {dims}"
                )
                errors.append({"provider": provider_name, "error": "Not registered", "dimensions": dims})
                provider_results.append(ProviderResult(
                    provider=provider_name,
                    latency_ms=0,
                    evaluations=[
                        EvaluationResult(
                            dimension=dim,
                            status="PASS",
                            score=1,
                            provider_metadata={
                                "note": (
                                    f"Provider '{provider_name}' is not registered in the "
                                    "Guardrail Service \u2014 fail-open PASS. "
                                    "Set MOCK_MODE=true in dev or configure provider credentials."
                                ),
                                "provider_not_registered": True,
                            },
                        )
                        for dim in dims
                    ],
                ))
                continue

            # Pass the provider's own fail_mode (read from config_providers at init)
            # so that fail_closed providers block on outage rather than passing.
            try:
                raw_results, latency_ms = provider.evaluate(
                    content=request.content,
                    evaluations=dims,
                    content_role=role,
                    fail_mode=provider.fail_mode,
                )
            except Exception as e:
                logger.error(f"Provider '{provider_name}' raised unhandled exception: {e}", exc_info=True)
                errors.append({"provider": provider_name, "error": str(e)})
                continue

            wrapped = []
            for r in raw_results:
                wrapped.append(EvaluationResult(
                    dimension=r["dimension"],
                    status=r.get("status", "UNKNOWN"),
                    score=r.get("score"),
                    provider_metadata=r.get("provider_metadata"),
                ))

            provider_results.append(ProviderResult(
                provider=provider_name,
                latency_ms=latency_ms,
                evaluations=wrapped,
            ))

            # Structured log per provider
            for r in wrapped:
                icon = {"FLAGGED": "\U0001f534", "WARNING": "\u26a0\ufe0f ", "PASS": "\u2705", "UNKNOWN": "\u2753"}.get(r.status, "  ")
                logger.info(
                    f"  {icon} [{provider_name}] {r.dimension:<20} "
                    f"score={str(r.score):<4} status={r.status}"
                )

        return TrustEvaluateResponse(
            request_id=request.request_id,
            provider_results=provider_results,
            errors=errors or None,
        )

    # ------------------------------------------------------------------ #
    #  GET /v1/providers                                                  #
    # ------------------------------------------------------------------ #
    @router.get("/v1/providers")
    def get_providers():
        """Return current provider registry state including active/inactive status."""
        return {"providers": provider_registry.as_catalog()}

    # ------------------------------------------------------------------ #
    #  GET /v1/evaluations                                               #
    # ------------------------------------------------------------------ #
    @router.get("/v1/evaluations")
    def get_evaluations():
        """Return the full evaluation taxonomy with active/deferred status per dimension."""
        if not eval_registry.is_healthy():
            return JSONResponse(
                status_code=503,
                content={
                    "error":   "service_config_unavailable",
                    "message": "Evaluation taxonomy could not be loaded from the PAP database.",
                    "detail":  eval_registry._startup_error,
                },
            )
        catalog = eval_registry.as_catalog()
        return {
            "total":    len(catalog),
            "active":   sum(1 for d in catalog if d["status"] == "active"),
            "deferred": sum(1 for d in catalog if d["status"] == "deferred"),
            "evaluations": catalog,
        }

    # ------------------------------------------------------------------ #
    #  GET /v1/health                                                     #
    # ------------------------------------------------------------------ #
    @router.get("/v1/health")
    def health():
        """
        Liveness/readiness check.
        Returns 200 when the service is fully operational.
        Returns 503 when in degraded mode (DB unavailable, no providers, etc.)
        with a structured error payload describing what failed.
        """
        import os as _os
        provider_statuses = [
            {"provider": name, "status": "healthy"}
            for name in provider_registry.all_providers()
        ]

        config_healthy = eval_registry.is_healthy()
        all_errors     = list(_startup_errors)   # copy; avoid mutation across requests

        body = {
            "status":              "healthy" if config_healthy and not all_errors else "degraded",
            "mock_mode":           _os.getenv("MOCK_MODE", "false").lower() in ("true", "1", "yes"),
            "providers":           provider_statuses,
            "active_dimensions":   len(eval_registry.active_dimensions()),
            "deferred_dimensions": len(eval_registry.deferred_dimensions()),
        }

        if all_errors:
            body["startup_errors"] = all_errors
        if eval_registry._startup_error:
            body["taxonomy_error"] = eval_registry._startup_error

        if not config_healthy or all_errors:
            body["remediation"] = (
                "Run pap/bootstrap.py to seed the PAP database, "
                "then restart guardrail-service."
            )
            return JSONResponse(status_code=503, content=body)

        return body

    # ------------------------------------------------------------------ #
    #  Corporate Policy — signal list CRUD                                #
    #  GET  /internal/corporate-policy/signals                           #
    #  PUT  /internal/corporate-policy/signals                           #
    # ------------------------------------------------------------------ #

    _SIGNALS_PATH = Path("config/policy_signals.yaml")

    @router.get("/internal/corporate-policy/signals")
    def get_corporate_policy_signals():
        """Return the current corporate_policy signal list and enforce_mode."""
        provider = provider_registry.get("local_policy_classifier")
        if provider is None:
            return JSONResponse(status_code=503, content={"error": "local_policy_classifier not registered"})

        # Return the live in-memory state (post-reload)
        keywords = provider._signals.get("corporate_policy", [])
        return {
            "enforce_mode": provider._enforce_mode,
            "keywords":     keywords,
            "count":        len(keywords),
            "source":       "file" if _SIGNALS_PATH.exists() else "defaults",
        }

    @router.put("/internal/corporate-policy/signals")
    def put_corporate_policy_signals(body: dict):
        """
        Persist a new signal list and enforce_mode, then hot-reload the classifier.

        Body: {"keywords": [...], "enforce_mode": "warn" | "block"}
        """
        provider = provider_registry.get("local_policy_classifier")
        if provider is None:
            return JSONResponse(status_code=503, content={"error": "local_policy_classifier not registered"})

        keywords     = body.get("keywords", [])
        enforce_mode = body.get("enforce_mode", "warn")

        if enforce_mode not in ("warn", "block"):
            return JSONResponse(status_code=422, content={"error": "enforce_mode must be 'warn' or 'block'"})

        cfg = {
            "enforce_mode": enforce_mode,
            "signals": {
                "corporate_policy": {
                    "keywords": [str(k).strip() for k in keywords if str(k).strip()],
                }
            },
        }

        try:
            _SIGNALS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_SIGNALS_PATH, "w") as f:
                yaml.dump(cfg, f, allow_unicode=True, sort_keys=False)
        except Exception as exc:
            logger.error(f"Failed to write policy_signals.yaml: {exc}")
            return JSONResponse(status_code=500, content={"error": str(exc)})

        try:
            provider.reload()
        except Exception as exc:
            logger.error(f"LocalPolicyClassifier reload error: {exc}")
            return JSONResponse(status_code=500, content={"error": f"Saved but reload failed: {exc}"})

        logger.info(f"corporate-policy signals updated: {len(keywords)} keywords, enforce_mode={enforce_mode}")
        return {
            "saved":         True,
            "count":         len(provider._signals.get("corporate_policy", [])),
            "enforce_mode":  provider._enforce_mode,
        }

    # ------------------------------------------------------------------ #
    #  POST /internal/mock-signals/reload                                 #
    # ------------------------------------------------------------------ #
    @router.post("/internal/mock-signals/reload")
    def reload_mock_signals():
        """
        Hot-reload MockProvider state.  MockProvider signals and scenarios are
        hardcoded constants (no YAML file), so this is a lightweight no-op that
        confirms the provider is registered and healthy.
        """
        mock_provider = provider_registry.get("mock_provider")
        if mock_provider is None:
            return JSONResponse(
                status_code=503,
                content={"error": "mock_provider is not registered"},
            )
        try:
            mock_provider.reload()
            return {
                "reloaded": True,
                "dimensions": len(mock_provider._signals),
                "scenarios":  len(mock_provider._scenarios),
            }
        except Exception as e:
            logger.error(f"mock-signals reload error: {e}")
            return JSONResponse(status_code=500, content={"error": str(e)})

    # ------------------------------------------------------------------ #
    #  POST /internal/taxonomy/reload                                      #
    # ------------------------------------------------------------------ #
    @router.post("/internal/taxonomy/reload")
    def reload_taxonomy():
        """
        Hot-reload the evaluation taxonomy from the PAP database.

        Picks up any changes to config_evaluation_dimensions without a service
        restart — useful when operators add supported_roles or change dimension
        status via the PAP UI or a migration.

        Returns the new active/deferred dimension counts.
        """
        try:
            eval_registry.reload()
        except Exception as e:
            logger.error(f"taxonomy reload error: {e}")
            return JSONResponse(status_code=500, content={"error": str(e)})

        if not eval_registry.is_healthy():
            return JSONResponse(
                status_code=503,
                content={
                    "error":   "taxonomy_reload_failed",
                    "detail":  eval_registry._startup_error,
                },
            )

        active   = len(eval_registry.active_dimensions())
        deferred = len(eval_registry.deferred_dimensions())
        logger.info(
            f"[taxonomy/reload] active={active} deferred={deferred}"
        )
        return {
            "reloaded":           True,
            "active_dimensions":  active,
            "deferred_dimensions": deferred,
        }

    return router
