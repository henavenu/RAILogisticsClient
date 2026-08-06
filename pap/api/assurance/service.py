"""
Assurance Service — FastAPI router for Tier 2 quality metrics (SRS §4.7).

The dashboard ALWAYS queries through this service — never directly from
providers.  This allows swapping providers transparently (SRS §4.7 principle).

Endpoints
─────────
  GET  /v1/assurance/metrics
       All T2 quality metrics (all providers, all apps).
       Supports ?app_id=&window= query params.
       Returns MetricResult list enriched with spec metadata.

  GET  /v1/assurance/metrics/{metric_id}
       Single metric, optionally filtered by app and window.

  GET  /v1/assurance/providers
       Registered provider list with supported metric IDs.

  POST /v1/assurance/compute
       Ad-hoc compute from an inline dataset.
       Body: { "metric_id": str, "dataset": [...], "context": {...} }

  GET  /v1/assurance/catalog
       Full metric catalog (all MetricSpec definitions) — useful for the UI
       to know thresholds, descriptions, and refresh schedules.

Design notes
────────────
  * Multiple providers may support the same metric_id (ensemble / A/B).
    By default the first registered provider wins; conflict resolution is
    left for Phase 3 (e.g., weighted average or primary/secondary config).
  * Provider registration happens at module import time via the
    module-level `assurance_service` singleton that PAP's main.py imports.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .provider import AssuranceProvider, MetricResult, MetricSpec, resolve_status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/assurance", tags=["Assurance (T2 Quality Metrics)"])


# ── Provider registry ─────────────────────────────────────────────────────────

class AssuranceProviderRegistry:
    """
    Lightweight registry for AssuranceProvider instances.

    Mirrors the GS ProviderRegistry pattern.
    Registration order matters — first provider wins for a given metric_id
    when multiple providers support it.
    """

    def __init__(self):
        self._providers: Dict[str, AssuranceProvider] = {}

    def register(self, provider: AssuranceProvider) -> None:
        name = provider.name()
        self._providers[name] = provider
        logger.info(
            f"[AssuranceService] Registered provider: {name} "
            f"({len(provider.supported_metrics())} metrics)"
        )

    def get(self, name: str) -> Optional[AssuranceProvider]:
        return self._providers.get(name)

    def all_providers(self) -> Dict[str, AssuranceProvider]:
        return dict(self._providers)

    def provider_for_metric(self, metric_id: str) -> Optional[AssuranceProvider]:
        """Return the first registered provider that supports metric_id."""
        for provider in self._providers.values():
            if metric_id in provider.supported_metrics():
                return provider
        return None

    def all_metrics(self) -> List[str]:
        """Union of all metric IDs across all registered providers."""
        seen = set()
        result = []
        for p in self._providers.values():
            for mid in p.supported_metrics():
                if mid not in seen:
                    seen.add(mid)
                    result.append(mid)
        return result


# ── Module-level singleton — shared across the process ────────────────────────

registry = AssuranceProviderRegistry()


def _result_to_dict(r: MetricResult) -> dict:
    """Serialise a MetricResult to a JSON-safe dict."""
    d = asdict(r)
    # Convert datetime to ISO string
    if isinstance(d.get("computed_at"), datetime):
        d["computed_at"] = d["computed_at"].isoformat()
    return d


def _full_catalog() -> Dict[str, dict]:
    """Merge catalogs from all registered providers."""
    catalog = {}
    for provider in registry.all_providers().values():
        for mid, spec in provider.metric_catalog().items():
            if mid not in catalog:
                catalog[mid] = asdict(spec)
    return catalog


# ── Pydantic request models ───────────────────────────────────────────────────

class ComputeRequest(BaseModel):
    metric_id: str
    dataset:   List[Dict[str, Any]]
    context:   Optional[Dict[str, Any]] = None


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/metrics")
def list_metrics(
    app_id: Optional[str] = Query(None, description="Filter metrics to a specific application"),
    window: str           = Query("24h", description="Time window: 1h | 6h | 24h | 7d | 30d"),
):
    """
    Return all Tier 2 quality metrics from all registered providers.

    Each entry includes the computed value, status (PASS/WARNING/FAIL),
    threshold bounds, and display metadata — ready for the dashboard.

    Response shape:
      {
        "window": "24h",
        "app_id": "mybank" | "all",
        "provider_count": 1,
        "metrics": [
          {
            "metric_id": "hallucination_rate",
            "display_name": "Hallucination Rate",
            "value": 3.2,
            "unit": "%",
            "status": "PASS",
            "sample_count": 1140,
            "threshold_warn": 5.0,
            "threshold_fail": 15.0,
            "better": "lower",
            "tier": 2,
            "refresh_schedule": "daily",
            "provider": "arize_phoenix",
            "computed_at": "2026-05-04T12:00:00+00:00"
          },
          ...
        ]
      }
    """
    if not registry.all_providers():
        return JSONResponse(
            status_code=503,
            content={"error": "No assurance providers registered"},
        )

    results = []
    for metric_id in registry.all_metrics():
        provider = registry.provider_for_metric(metric_id)
        if provider:
            result = provider.safe_query(metric_id, app_id=app_id, time_range=window)
            results.append(_result_to_dict(result))

    return {
        "window":         window,
        "app_id":         app_id or "all",
        "provider_count": len(registry.all_providers()),
        "metric_count":   len(results),
        "metrics":        results,
    }


@router.get("/metrics/{metric_id}")
def get_metric(
    metric_id: str,
    app_id:    Optional[str] = Query(None),
    window:    str           = Query("24h"),
):
    """
    Return a single Tier 2 quality metric by ID.

    Raises 404 if no provider supports the requested metric_id.
    """
    provider = registry.provider_for_metric(metric_id)
    if not provider:
        raise HTTPException(
            status_code=404,
            detail=f"No provider supports metric_id={metric_id!r}. "
                   f"Available: {registry.all_metrics()}",
        )

    result = provider.safe_query(metric_id, app_id=app_id, time_range=window)
    return _result_to_dict(result)


@router.get("/providers")
def list_providers():
    """
    Return registered Assurance providers with their supported metric IDs.
    """
    providers = []
    for name, provider in registry.all_providers().items():
        providers.append({
            "name":              name,
            "supported_metrics": provider.supported_metrics(),
            "metric_count":      len(provider.supported_metrics()),
        })
    return {
        "provider_count": len(providers),
        "providers":      providers,
        "all_metrics":    registry.all_metrics(),
    }


@router.get("/catalog")
def get_catalog():
    """
    Return the full metric catalog — MetricSpec definitions from all providers.

    Useful for the UI to know thresholds, descriptions, and refresh schedules
    without making individual metric queries.
    """
    catalog = _full_catalog()
    return {
        "metric_count": len(catalog),
        "metrics":      list(catalog.values()),
    }


@router.post("/compute")
def compute_metric(req: ComputeRequest):
    """
    Ad-hoc metric computation from an inline dataset.

    Useful for:
      - Offline test suite evaluation (hundreds of examples)
      - Immediate feedback without waiting for the daily refresh cycle
      - CI/CD quality gate checks

    Request body:
      {
        "metric_id": "hallucination_rate",
        "dataset": [
          {"is_hallucination": false, "groundedness": 0.92},
          {"is_hallucination": true,  "groundedness": 0.31},
          ...
        ],
        "context": {"app_id": "mybank", "time_range": "24h"}
      }

    Dataset record keys by metric_id:
      hallucination_rate   → is_hallucination: bool
      groundedness_score   → groundedness: float (0–1)
      retrieval_precision  → retrieved_count: int, relevant_count: int
      response_relevance   → relevance: float (0–1)
      toxicity_rate        → is_toxic: bool
      context_utilisation  → utilisation: float (0–1)
    """
    provider = registry.provider_for_metric(req.metric_id)
    if not provider:
        raise HTTPException(
            status_code=404,
            detail=f"No provider supports metric_id={req.metric_id!r}. "
                   f"Available: {registry.all_metrics()}",
        )

    result = provider.safe_compute(req.metric_id, req.dataset, req.context)
    return _result_to_dict(result)


@router.post("/compute/batch")
def compute_batch(
    dataset: List[Dict[str, Any]],
    context: Optional[Dict[str, Any]] = None,
):
    """
    Compute ALL supported metrics from a single shared dataset.

    Useful for the offline test suite — submit one batch of evaluation records
    and receive quality scores across all metrics simultaneously.

    Dataset records should include all keys needed by all metrics:
      {
        "is_hallucination": bool,
        "groundedness": float,
        "retrieved_count": int,
        "relevant_count": int,
        "relevance": float,
        "is_toxic": bool,
        "utilisation": float
      }
    """
    context = context or {}
    results = []

    for metric_id in registry.all_metrics():
        provider = registry.provider_for_metric(metric_id)
        if provider:
            result = provider.safe_compute(metric_id, dataset, context)
            results.append(_result_to_dict(result))

    return {
        "metric_count":  len(results),
        "dataset_size":  len(dataset),
        "context":       context,
        "computed_at":   datetime.now(timezone.utc).isoformat(),
        "metrics":       results,
    }
