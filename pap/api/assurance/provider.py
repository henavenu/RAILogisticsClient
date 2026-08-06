"""
AssuranceProvider SPI (Service Provider Interface) — SRS §4.7.2.

Mirrors the Guardrail Service TrustProvider pattern:

  TrustProvider (GS)          AssuranceProvider (here)
  ─────────────────────────── ──────────────────────────────────────────
  name()                      name()
  _evaluate(content, dims)    compute(metric_id, dataset, context)
  evaluate() — safe wrapper   query(metric_id, app_id, time_range)
  ProviderRegistry            (registered in AssuranceService)

Contract
--------
Subclasses implement compute() and query() for the happy path.
The base class never propagates exceptions — failures return a MetricResult
with value=None and an error string, so callers always get a typed response.

Usage
-----
  provider = ArizePhoenixProvider()
  result = provider.query("hallucination_rate", app_id="mybank", time_range="24h")
  print(result.value, result.status)
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Metric catalog entry ──────────────────────────────────────────────────────

@dataclass
class MetricSpec:
    """
    Static descriptor for a quality metric.

    better: "higher" means higher value is better (groundedness, precision).
            "lower"  means lower value is better (hallucination rate, toxicity).
    """
    metric_id:        str
    display_name:     str
    description:      str
    unit:             str         # "%", "score", "count", "ms"
    tier:             int         # 2 or 3
    refresh_schedule: str         # "hourly" | "daily" | "weekly"
    better:           str         # "higher" | "lower"
    threshold_warn:   float       # value at which status → WARNING
    threshold_fail:   float       # value at which status → FAIL


# ── MetricResult ──────────────────────────────────────────────────────────────

@dataclass
class MetricResult:
    """
    Single resolved metric value returned by any AssuranceProvider.

    The `status` field follows the same PASS / WARNING / FAIL convention used
    throughout the platform (consistent with Guardrail Service verdicts).
    """
    metric_id:    str
    value:        Optional[float]
    unit:         str
    window:       str                    # "1h" | "6h" | "24h" | "7d" | "30d"
    status:       str = "UNKNOWN"        # PASS | WARNING | FAIL | UNKNOWN
    sample_count: int = 0
    computed_at:  datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    provider:     str = ""
    confidence:   Optional[float] = None   # 0–1 confidence in the estimate
    metadata:     Dict[str, Any] = field(default_factory=dict)
    error:        Optional[str] = None     # populated when value=None on failure

    # ── Derived fields (populated by AssuranceProvider.resolve_status) ────────
    display_name: str = ""
    description:  str = ""
    tier:         int = 2
    refresh_schedule: str = ""
    better:       str = "higher"          # "higher" | "lower"
    threshold_warn:  Optional[float] = None
    threshold_fail:  Optional[float] = None


def resolve_status(result: MetricResult, spec: MetricSpec) -> MetricResult:
    """
    Compute PASS / WARNING / FAIL based on MetricSpec thresholds.
    Copies human-readable fields from the spec into the result.
    """
    result.display_name     = spec.display_name
    result.description      = spec.description
    result.tier             = spec.tier
    result.refresh_schedule = spec.refresh_schedule
    result.better           = spec.better
    result.threshold_warn   = spec.threshold_warn
    result.threshold_fail   = spec.threshold_fail

    if result.value is None or result.error:
        result.status = "UNKNOWN"
        return result

    v = result.value
    if spec.better == "higher":
        if v >= spec.threshold_warn:
            result.status = "PASS"
        elif v >= spec.threshold_fail:
            result.status = "WARNING"
        else:
            result.status = "FAIL"
    else:
        # lower is better
        if v <= spec.threshold_warn:
            result.status = "PASS"
        elif v <= spec.threshold_fail:
            result.status = "WARNING"
        else:
            result.status = "FAIL"

    return result


# ── AssuranceProvider ABC ─────────────────────────────────────────────────────

class AssuranceProvider(ABC):
    """
    Base class for all Assurance Provider adapters (SRS §4.7.2).

    Subclasses must implement:
      - name()               — stable identifier (registry key)
      - supported_metrics()  — list of metric IDs this provider handles
      - compute()            — ad-hoc metric from a caller-supplied dataset
      - query()              — fetch pre-computed metric from provider's store

    Subclasses MUST NOT override:
      - safe_query()         — owned by base; wraps query() with error handling
      - safe_compute()       — owned by base; wraps compute() with error handling
      - query_all()          — convenience batch; calls safe_query() in a loop
    """

    @abstractmethod
    def name(self) -> str:
        """Unique, stable identifier. Used as registry key."""

    @abstractmethod
    def supported_metrics(self) -> List[str]:
        """Return metric IDs this provider can compute or query."""

    @abstractmethod
    def metric_catalog(self) -> Dict[str, MetricSpec]:
        """Return the full MetricSpec catalog keyed by metric_id."""

    @abstractmethod
    def compute(
        self,
        metric_id: str,
        dataset:   List[Dict[str, Any]],
        context:   Optional[Dict[str, Any]] = None,
    ) -> MetricResult:
        """
        Ad-hoc compute metric_id from caller-supplied evaluation records.

        dataset: list of dicts, each representing one evaluated interaction.
                 Expected keys vary by metric_id — see provider docs.
        context: optional extra parameters (app_id, model, date_range, etc.)
        """

    @abstractmethod
    def query(
        self,
        metric_id:  str,
        app_id:     Optional[str] = None,
        time_range: str = "24h",
    ) -> MetricResult:
        """
        Fetch pre-computed metric from the provider's observability store
        (e.g. Arize Phoenix, LangSmith, W&B).

        In mock / bootstrap mode providers return hardcoded seed values.
        In production this makes a live API call to the backing service.
        """

    # ── Safe wrappers (final — do not override) ───────────────────────────────

    def safe_query(
        self,
        metric_id:  str,
        app_id:     Optional[str] = None,
        time_range: str = "24h",
    ) -> MetricResult:
        """Exception-safe wrapper around query(). Never propagates."""
        try:
            return self.query(metric_id, app_id=app_id, time_range=time_range)
        except Exception as exc:
            logger.error(
                f"[{self.name()}] query({metric_id!r}) failed: {exc}",
                exc_info=True,
            )
            return MetricResult(
                metric_id=metric_id,
                value=None,
                unit="unknown",
                window=time_range,
                provider=self.name(),
                error=str(exc),
                status="UNKNOWN",
            )

    def safe_compute(
        self,
        metric_id: str,
        dataset:   List[Dict[str, Any]],
        context:   Optional[Dict[str, Any]] = None,
    ) -> MetricResult:
        """Exception-safe wrapper around compute(). Never propagates."""
        try:
            return self.compute(metric_id, dataset, context)
        except Exception as exc:
            logger.error(
                f"[{self.name()}] compute({metric_id!r}) failed: {exc}",
                exc_info=True,
            )
            return MetricResult(
                metric_id=metric_id,
                value=None,
                unit="unknown",
                window=(context or {}).get("time_range", "unknown"),
                provider=self.name(),
                error=str(exc),
                status="UNKNOWN",
            )

    def query_all(
        self,
        app_id:     Optional[str] = None,
        time_range: str = "24h",
    ) -> List[MetricResult]:
        """
        Convenience: query all supported metrics in one call.

        Default implementation loops over supported_metrics() calling
        safe_query() for each.  Override for batch-capable providers.
        """
        catalog = self.metric_catalog()
        results = []
        for mid in self.supported_metrics():
            result = self.safe_query(mid, app_id=app_id, time_range=time_range)
            spec = catalog.get(mid)
            if spec:
                result = resolve_status(result, spec)
            results.append(result)
        return results
