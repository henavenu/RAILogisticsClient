"""
MockAssuranceProvider — AssuranceProvider that proxies to the mock assurance
service (proxy_assurance.py) running at ASSURANCE_URL (default: http://127.0.0.1:3010).

This provider translates the mock's raw response shape into the full MetricResult
that the PAP assurance service and dashboard expect (including PASS/WARNING/FAIL
status, thresholds, and display metadata).

Metric ID mapping
─────────────────
The mock proxy uses "toxicity_judge_rate" while the dashboard and MetricSpec
catalog use "toxicity_rate".  This provider fetches "toxicity_judge_rate" from
the mock and returns it as "toxicity_rate" so the dashboard label map and
normalisation logic work without changes.

Supported metrics (6 of the 8 mock metrics):
  hallucination_rate   → mock: hallucination_rate    (same)
  groundedness_score   → mock: groundedness_score    (same)
  retrieval_precision  → mock: retrieval_precision   (same)
  response_relevance   → mock: response_relevance    (same)
  toxicity_rate        → mock: toxicity_judge_rate   (remapped)
  context_utilisation  → mock: context_utilisation   (same)

The remaining mock metrics (prompt_injection_rate, pii_exposure_rate) are not
in the dashboard's Tier 2 panel and are not surfaced by this provider.

Replacing with a live provider
──────────────────────────────
Set ASSURANCE_URL in pap/.env (or the root .env) to the real provider's base URL.
The mock proxy will not be launched by start_all.bat when ASSURANCE_URL does not
point to 127.0.0.1:3010 or localhost:3010.  Replace this class with a provider
that speaks your real provider's API contract.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import httpx

from .arize_provider import _METRIC_CATALOG
from .provider import AssuranceProvider, MetricResult, MetricSpec, resolve_status

logger = logging.getLogger(__name__)

# ── Metric ID remapping ────────────────────────────────────────────────────────
# Maps catalog metric_id → the metric_id used by the mock proxy.
# Only entries that differ need to appear here.

_MOCK_ID_MAP: Dict[str, str] = {
    "toxicity_rate": "toxicity_judge_rate",
}

# ── HTTP client config ─────────────────────────────────────────────────────────

_DEFAULT_ASSURANCE_URL = "http://127.0.0.1:3010"
_HTTP_TIMEOUT          = 8.0    # seconds — gives the slow scenario room to breathe


# ── MockAssuranceProvider ──────────────────────────────────────────────────────

class MockAssuranceProvider(AssuranceProvider):
    """
    AssuranceProvider that forwards queries to the mock assurance proxy.

    Reads ASSURANCE_URL from the process environment at query-time so that
    start_all.bat / pap/.env overrides are picked up without reloading.
    """

    def name(self) -> str:
        return "mock_assurance"

    def supported_metrics(self) -> List[str]:
        return list(_METRIC_CATALOG.keys())

    def metric_catalog(self) -> Dict[str, MetricSpec]:
        return _METRIC_CATALOG

    # ── Private helpers ───────────────────────────────────────────────────────

    def _assurance_url(self) -> str:
        return os.getenv("ASSURANCE_URL", _DEFAULT_ASSURANCE_URL).rstrip("/")

    def _fetch_from_mock(
        self,
        catalog_metric_id: str,
        app_id:     Optional[str],
        time_range: str,
    ) -> tuple:
        """
        Call the mock proxy's single-metric endpoint.

        Returns (value, data_status, error_detail).
        value is None when data_status != "ok".

        The mock only supports 1h / 6h / 24h / 7d windows.
        30d is mapped to 7d as the closest supported window.
        """
        mock_metric_id = _MOCK_ID_MAP.get(catalog_metric_id, catalog_metric_id)
        window         = time_range if time_range in {"1h", "6h", "24h", "7d"} else "7d"
        app_id_param   = app_id or "all"

        url = (
            f"{self._assurance_url()}/v1/metrics/{mock_metric_id}"
            f"?app_id={app_id_param}&window={window}"
        )

        logger.debug(f"[MockAssurance] GET {url}")
        resp = httpx.get(url, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()

        # Payload shape:
        #   { "app_id": ..., "window": ..., "data_status": ...,
        #     "metric_count": 1, "metrics": [{ "metric_id", "value", "unit",
        #                                       "data_status", "error" }] }
        if not payload.get("metrics"):
            return None, "no_data", "Empty metrics array in mock response"

        m = payload["metrics"][0]
        return m.get("value"), m.get("data_status", "ok"), m.get("error")

    def _build_no_value_result(
        self,
        metric_id:   str,
        spec:        MetricSpec,
        time_range:  str,
        data_status: str,
        error:       Optional[str],
    ) -> MetricResult:
        """
        Build a MetricResult for a metric that returned no numeric value.
        Populates spec metadata so the UI can still render the tile header.
        """
        result = MetricResult(
            metric_id = metric_id,
            value     = None,
            unit      = spec.unit,
            window    = time_range,
            provider  = self.name(),
            error     = error or f"data_status={data_status}",
            status    = "UNKNOWN",
            metadata  = {
                "data_status": data_status,
                "mock_id":     _MOCK_ID_MAP.get(metric_id, metric_id),
            },
        )
        # Populate spec metadata manually (resolve_status is skipped when value=None,
        # but we want display_name / tier / thresholds available for the UI)
        result.display_name      = spec.display_name
        result.description       = spec.description
        result.tier              = spec.tier
        result.refresh_schedule  = spec.refresh_schedule
        result.better            = spec.better
        result.threshold_warn    = spec.threshold_warn
        result.threshold_fail    = spec.threshold_fail
        return result

    # ── SPI implementation — query ────────────────────────────────────────────

    def query(
        self,
        metric_id:  str,
        app_id:     Optional[str] = None,
        time_range: str = "24h",
    ) -> MetricResult:
        """
        Fetch a metric from the mock assurance proxy and return a full MetricResult.

        Raises ValueError for unknown metric IDs (base class safe_query() catches
        all exceptions and returns a UNKNOWN MetricResult, so this never propagates
        to the HTTP layer).
        """
        spec = _METRIC_CATALOG.get(metric_id)
        if not spec:
            raise ValueError(f"Unknown metric_id: {metric_id!r}")

        value, data_status, error_detail = self._fetch_from_mock(
            metric_id, app_id, time_range
        )

        if data_status != "ok" or value is None:
            return self._build_no_value_result(
                metric_id, spec, time_range, data_status, error_detail
            )

        result = MetricResult(
            metric_id    = metric_id,
            value        = value,
            unit         = spec.unit,
            window       = time_range,
            provider     = self.name(),
            metadata     = {
                "data_status": data_status,
                "mock_id":     _MOCK_ID_MAP.get(metric_id, metric_id),
                "app_id":      app_id or "all",
            },
        )
        return resolve_status(result, spec)

    # ── SPI implementation — compute ──────────────────────────────────────────

    def compute(
        self,
        metric_id: str,
        dataset:   List[Dict[str, Any]],
        context:   Optional[Dict[str, Any]] = None,
    ) -> MetricResult:
        """
        MockAssuranceProvider does not support ad-hoc compute — it is a
        read-only proxy to the mock HTTP service.

        Returns an UNKNOWN MetricResult so callers degrade gracefully.
        """
        spec    = _METRIC_CATALOG.get(metric_id)
        unit    = spec.unit if spec else "unknown"
        context = context or {}

        return MetricResult(
            metric_id = metric_id,
            value     = None,
            unit      = unit,
            window    = context.get("time_range", "24h"),
            provider  = self.name(),
            error     = (
                "MockAssuranceProvider does not support ad-hoc compute. "
                "Use the query() path (GET /v1/assurance/metrics) instead."
            ),
            status    = "UNKNOWN",
        )
