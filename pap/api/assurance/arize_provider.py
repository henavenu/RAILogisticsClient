"""
ArizePhoenixProvider — AssuranceProvider backed by Arize Phoenix.

SRS §4.7.3 — Tier 2 quality metrics, daily refresh, LLM-judge source.

─────────────────────────────────────────────────────────────────────────────
IMPLEMENTATION PHASES
─────────────────────────────────────────────────────────────────────────────

Phase 1 (current): Hardcoded seed values.
  _fetch_from_arize() returns HARDCODED_VALUES[metric_id][time_range].
  Zero external dependencies — the service starts with no Arize credentials.

Phase 2: Derive from local audit data.
  _fetch_from_arize() queries audit_evaluation_dimensions via SQLAlchemy,
  computing hallucination / groundedness proxies from guardrail scores.

Phase 3: Live Arize Phoenix integration.
  _fetch_from_arize() sends a GraphQL request to ${ARIZE_PHOENIX_URL}/graphql.
  Credential: ARIZE_PHOENIX_API_KEY env var.

─────────────────────────────────────────────────────────────────────────────
T2 METRIC CATALOG (SRS §4.7.3)
─────────────────────────────────────────────────────────────────────────────

  Metric ID             Unit    Refresh   Source          Better
  ──────────────────── ─────── ───────── ─────────────── ───────
  hallucination_rate    %       daily     LLM-judge       lower
  groundedness_score    score   daily     LLM-judge       higher
  retrieval_precision   %       daily     LLM-judge       higher
  response_relevance    score   daily     LLM-judge       higher
  toxicity_rate         %       hourly    LLM-judge       lower
  context_utilisation   %       daily     computed        higher

─────────────────────────────────────────────────────────────────────────────
ADDING A REAL ARIZE INTEGRATION (Phase 3 stub)
─────────────────────────────────────────────────────────────────────────────

When Arize Phoenix is available, replace _fetch_from_arize() with:

  import os, httpx

  PHOENIX_URL = os.getenv("ARIZE_PHOENIX_URL", "http://localhost:6006")
  PHOENIX_KEY = os.getenv("ARIZE_PHOENIX_API_KEY", "")

  PHOENIX_QUERY = '''
    query MetricQuery($metricId: String!, $startTime: DateTime!, $endTime: DateTime!, $appId: String) {
      model(name: $appId) {
        metric(name: $metricId, startTime: $startTime, endTime: $endTime) {
          value
          sampleCount
          computedAt
        }
      }
    }
  '''

  def _fetch_from_arize(self, metric_id, app_id, time_range):
      end   = datetime.now(timezone.utc)
      start = end - _WINDOW_DELTA[time_range]
      r = httpx.post(
          f"{PHOENIX_URL}/graphql",
          headers={"Authorization": f"Bearer {PHOENIX_KEY}"},
          json={"query": PHOENIX_QUERY, "variables": {
              "metricId": metric_id,
              "startTime": start.isoformat(),
              "endTime":   end.isoformat(),
              "appId":     app_id or "all",
          }},
          timeout=10.0,
      )
      r.raise_for_status()
      data = r.json()["data"]["model"]["metric"]
      return data["value"], int(data["sampleCount"])
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .provider import AssuranceProvider, MetricResult, MetricSpec, resolve_status

logger = logging.getLogger(__name__)

# ── Time-window helpers ────────────────────────────────────────────────────────

_WINDOW_DELTA: Dict[str, timedelta] = {
    "1h":  timedelta(hours=1),
    "6h":  timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d":  timedelta(days=7),
    "30d": timedelta(days=30),
}


# ── T2 Metric catalog ─────────────────────────────────────────────────────────

_METRIC_CATALOG: Dict[str, MetricSpec] = {

    # Hallucination rate — % of responses containing factual fabrications.
    # Evaluated by an LLM judge that cross-checks the response against
    # the retrieved context and known facts.
    "hallucination_rate": MetricSpec(
        metric_id        = "hallucination_rate",
        display_name     = "Hallucination Rate",
        description      = (
            "Percentage of AI responses containing factual fabrications or "
            "unsupported claims, as detected by LLM judge evaluation."
        ),
        unit             = "%",
        tier             = 2,
        refresh_schedule = "daily",
        better           = "lower",
        threshold_warn   = 5.0,    # > 5% → WARNING
        threshold_fail   = 15.0,   # > 15% → FAIL
    ),

    # Groundedness score — 0–1, how well grounded the response is to context.
    # A score of 1.0 means every factual claim is supported by retrieved docs.
    "groundedness_score": MetricSpec(
        metric_id        = "groundedness_score",
        display_name     = "Groundedness Score",
        description      = (
            "Average score (0–1) measuring how well AI responses are grounded "
            "in the retrieved context documents, as rated by LLM judge."
        ),
        unit             = "score",
        tier             = 2,
        refresh_schedule = "daily",
        better           = "higher",
        threshold_warn   = 0.75,   # < 0.75 → WARNING
        threshold_fail   = 0.60,   # < 0.60 → FAIL
    ),

    # Retrieval precision — % of retrieved chunks that were actually relevant.
    # Measures quality of the RAG retrieval stage, not the generation.
    "retrieval_precision": MetricSpec(
        metric_id        = "retrieval_precision",
        display_name     = "Retrieval Precision",
        description      = (
            "Percentage of retrieved context documents that were relevant "
            "to the user's query, evaluating RAG retrieval quality."
        ),
        unit             = "%",
        tier             = 2,
        refresh_schedule = "daily",
        better           = "higher",
        threshold_warn   = 70.0,   # < 70% → WARNING
        threshold_fail   = 50.0,   # < 50% → FAIL
    ),

    # Response relevance — 0–1, does the response address the user's question.
    "response_relevance": MetricSpec(
        metric_id        = "response_relevance",
        display_name     = "Response Relevance",
        description      = (
            "Average score (0–1) measuring how well the AI response addresses "
            "the user's original question or intent."
        ),
        unit             = "score",
        tier             = 2,
        refresh_schedule = "daily",
        better           = "higher",
        threshold_warn   = 0.70,   # < 0.70 → WARNING
        threshold_fail   = 0.50,   # < 0.50 → FAIL
    ),

    # Toxicity rate — % of responses flagged as harmful/toxic.
    "toxicity_rate": MetricSpec(
        metric_id        = "toxicity_rate",
        display_name     = "Toxicity Rate",
        description      = (
            "Percentage of AI responses containing harmful, offensive, or "
            "toxic language as detected by the safety evaluator."
        ),
        unit             = "%",
        tier             = 2,
        refresh_schedule = "hourly",
        better           = "lower",
        threshold_warn   = 1.0,    # > 1% → WARNING
        threshold_fail   = 5.0,    # > 5% → FAIL
    ),

    # Context utilisation — % of the retrieved context actually referenced.
    "context_utilisation": MetricSpec(
        metric_id        = "context_utilisation",
        display_name     = "Context Utilisation",
        description      = (
            "Percentage of the retrieved context that was meaningfully "
            "referenced in the response, measuring retrieval efficiency."
        ),
        unit             = "%",
        tier             = 2,
        refresh_schedule = "daily",
        better           = "higher",
        threshold_warn   = 60.0,   # < 60% → WARNING
        threshold_fail   = 40.0,   # < 40% → FAIL
    ),
}

# ── Phase 1 seed values ────────────────────────────────────────────────────────
#
# Realistic baseline values for a financial-domain conversational AI.
# Per-window values reflect natural variation (longer windows = more stable).
# Replace with _fetch_from_arize() when Phoenix integration is available.
#
# Structure: { metric_id: { time_range: (value, sample_count) } }

_SEED_VALUES: Dict[str, Dict[str, tuple]] = {
    "hallucination_rate": {
        "1h":  (2.1,   48),
        "6h":  (2.8,  285),
        "24h": (3.2, 1140),
        "7d":  (4.1, 7980),
        "30d": (5.3, 34200),
    },
    "groundedness_score": {
        "1h":  (0.891,  48),
        "6h":  (0.882, 285),
        "24h": (0.874, 1140),
        "7d":  (0.851, 7980),
        "30d": (0.843, 34200),
    },
    "retrieval_precision": {
        "1h":  (82.4,   48),
        "6h":  (80.7,  285),
        "24h": (79.4, 1140),
        "7d":  (77.2, 7980),
        "30d": (75.6, 34200),
    },
    "response_relevance": {
        "1h":  (0.844,  48),
        "6h":  (0.831, 285),
        "24h": (0.819, 1140),
        "7d":  (0.803, 7980),
        "30d": (0.791, 34200),
    },
    "toxicity_rate": {
        "1h":  (0.2,   48),
        "6h":  (0.3,  285),
        "24h": (0.4, 1140),
        "7d":  (0.6, 7980),
        "30d": (0.7, 34200),
    },
    "context_utilisation": {
        "1h":  (73.1,   48),
        "6h":  (71.4,  285),
        "24h": (69.8, 1140),
        "7d":  (68.2, 7980),
        "30d": (66.5, 34200),
    },
}

# Per-app jitter offsets (added to global values) to simulate real per-app spread.
# Positive = this app's metric is worse than global; negative = better.
_APP_OFFSETS: Dict[str, Dict[str, float]] = {
    "mybank": {
        "hallucination_rate":  -0.8,   # MyBank LLM is better calibrated
        "groundedness_score":   0.012,
        "retrieval_precision":  1.6,
        "response_relevance":   0.015,
        "toxicity_rate":       -0.1,
        "context_utilisation":  2.3,
    },
}


# ── ArizePhoenixProvider ──────────────────────────────────────────────────────

class ArizePhoenixProvider(AssuranceProvider):
    """
    Assurance Provider backed by Arize Phoenix (SRS §4.7.2).

    Phase 1: returns hardcoded seed values (no external calls).
    See module docstring for Phase 2/3 upgrade path.
    """

    def name(self) -> str:
        return "arize_phoenix"

    def supported_metrics(self) -> List[str]:
        return list(_METRIC_CATALOG.keys())

    def metric_catalog(self) -> Dict[str, MetricSpec]:
        return _METRIC_CATALOG

    # ── Phase 1 fetch — hardcoded seed values ─────────────────────────────────

    def _fetch_from_arize(
        self,
        metric_id:  str,
        app_id:     Optional[str],
        time_range: str,
    ) -> tuple:
        """
        Return (value, sample_count) for the given metric / window.

        Phase 1: reads from _SEED_VALUES with optional per-app offset + tiny
                 random jitter so successive calls look like live data.

        Phase 3: replace this method body with a real Phoenix GraphQL query.
        See module docstring for the full Phase 3 stub.
        """
        base_values = _SEED_VALUES.get(metric_id, {})
        base_pair   = base_values.get(time_range, (None, 0))
        base_val, base_samples = base_pair

        if base_val is None:
            return None, 0

        # Per-app offset
        offset = 0.0
        if app_id:
            app_offsets = _APP_OFFSETS.get(app_id.lower(), {})
            offset = app_offsets.get(metric_id, 0.0)

        # Small deterministic jitter so each call isn't identical
        # (simulates natural variation in live metrics)
        spec = _METRIC_CATALOG[metric_id]
        jitter_range = spec.threshold_warn * 0.02   # ±2% of the warn threshold
        jitter = random.uniform(-jitter_range, jitter_range)

        value = round(base_val + offset + jitter, 4)

        # Scale sample count if filtering to a single app (rough ~10% of total)
        samples = base_samples if not app_id else max(5, int(base_samples * 0.1))

        return value, samples

    # ── SPI implementation — compute ──────────────────────────────────────────

    def compute(
        self,
        metric_id: str,
        dataset:   List[Dict[str, Any]],
        context:   Optional[Dict[str, Any]] = None,
    ) -> MetricResult:
        """
        Ad-hoc metric computation from a caller-supplied evaluation dataset.

        dataset: list of dicts with keys that depend on metric_id.
          For hallucination_rate / groundedness_score / response_relevance:
            {"is_hallucination": bool, "groundedness": float,
             "relevance": float, "has_context": bool}
          For retrieval_precision:
            {"retrieved_count": int, "relevant_count": int}

        Falls back to a seed-derived estimate if dataset is empty.
        """
        context   = context or {}
        time_range = context.get("time_range", "24h")
        app_id     = context.get("app_id")
        spec       = _METRIC_CATALOG.get(metric_id)

        if not spec:
            raise ValueError(f"Unknown metric_id: {metric_id!r}")

        if not dataset:
            # No dataset supplied — fall back to stored estimate
            value, samples = self._fetch_from_arize(metric_id, app_id, time_range)
            result = MetricResult(
                metric_id    = metric_id,
                value        = value,
                unit         = spec.unit,
                window       = time_range,
                sample_count = samples,
                provider     = self.name(),
                metadata     = {"source": "seed_fallback", "reason": "empty dataset"},
            )
            return resolve_status(result, spec)

        n = len(dataset)
        value: Optional[float] = None

        if metric_id == "hallucination_rate":
            flagged = sum(1 for r in dataset if r.get("is_hallucination", False))
            value = round(flagged / n * 100, 2) if n else None

        elif metric_id == "groundedness_score":
            scores = [r["groundedness"] for r in dataset if "groundedness" in r]
            value = round(sum(scores) / len(scores), 4) if scores else None

        elif metric_id == "retrieval_precision":
            total_ret = sum(r.get("retrieved_count", 0) for r in dataset)
            total_rel = sum(r.get("relevant_count",  0) for r in dataset)
            value = round(total_rel / total_ret * 100, 2) if total_ret else None

        elif metric_id == "response_relevance":
            scores = [r["relevance"] for r in dataset if "relevance" in r]
            value = round(sum(scores) / len(scores), 4) if scores else None

        elif metric_id == "toxicity_rate":
            flagged = sum(1 for r in dataset if r.get("is_toxic", False))
            value = round(flagged / n * 100, 2) if n else None

        elif metric_id == "context_utilisation":
            scores = [r["utilisation"] for r in dataset if "utilisation" in r]
            value = round(sum(scores) / len(scores) * 100, 2) if scores else None

        else:
            raise ValueError(f"compute() not implemented for metric_id: {metric_id!r}")

        result = MetricResult(
            metric_id    = metric_id,
            value        = value,
            unit         = spec.unit,
            window       = time_range,
            sample_count = n,
            provider     = self.name(),
            metadata     = {"source": "ad_hoc_compute", "dataset_size": n},
        )
        return resolve_status(result, spec)

    # ── SPI implementation — query ────────────────────────────────────────────

    def query(
        self,
        metric_id:  str,
        app_id:     Optional[str] = None,
        time_range: str = "24h",
    ) -> MetricResult:
        """
        Fetch pre-computed metric value from Arize Phoenix.

        Phase 1: delegates to _fetch_from_arize() which returns seed values.
        Phase 3: _fetch_from_arize() makes the live Phoenix GraphQL call.
        """
        spec = _METRIC_CATALOG.get(metric_id)
        if not spec:
            raise ValueError(f"Unknown metric_id: {metric_id!r}")
        if metric_id not in self.supported_metrics():
            raise ValueError(f"Metric {metric_id!r} not supported by {self.name()}")

        value, samples = self._fetch_from_arize(metric_id, app_id, time_range)

        result = MetricResult(
            metric_id    = metric_id,
            value        = value,
            unit         = spec.unit,
            window       = time_range,
            sample_count = samples,
            provider     = self.name(),
            metadata     = {
                "source":   "arize_phoenix",
                "phase":    "seed",          # change to "live" in Phase 3
                "app_id":   app_id or "all",
            },
        )
        return resolve_status(result, spec)
