"""
Mock Assurance Provider — implements the real Assurance v0.4 HTTP contract.

FastAPI service on port 3010.  Five runtime-switchable scenarios:

  ok                   All metrics return realistic seed values  (default)
  no_data              All metrics return value=null, data_status=no_data
  provider_unavailable All metrics fail; /v1/healthz reports degraded
  partial              First half ok, second half no_data; envelope=partial
  slow                 5-second sleep before every response (timeout testing)

Switch scenario at runtime:
  POST /v1/_test/scenario?scenario=no_data
  GET  /v1/_test/scenario

The _test endpoints are NOT part of the real Assurance v0.4 contract.
Never call them from production code.
"""

import os
import time
from typing import List, Optional

import uvicorn
from fastapi import FastAPI, Path, Query
from fastapi.responses import JSONResponse

# ── Metric catalog ─────────────────────────────────────────────────────────────
# Stable ordering matters: "partial" scenario splits on index position.

CATALOG: List[dict] = [
    {"metric_id": "hallucination_rate",    "unit": "%"},
    {"metric_id": "groundedness_score",    "unit": "score"},
    {"metric_id": "response_relevance",    "unit": "score"},
    {"metric_id": "toxicity_judge_rate",   "unit": "%"},
    {"metric_id": "prompt_injection_rate", "unit": "%"},
    {"metric_id": "pii_exposure_rate",     "unit": "%"},
    {"metric_id": "retrieval_precision",   "unit": "%"},
    {"metric_id": "context_utilisation",   "unit": "%"},
]

# Realistic seed values per time window.  Values drift naturally across windows.
SEED: dict = {
    "hallucination_rate":    {"1h": 2.1,   "6h": 2.8,   "24h": 3.2,   "7d": 4.1},
    "groundedness_score":    {"1h": 0.891, "6h": 0.882, "24h": 0.874, "7d": 0.851},
    "response_relevance":    {"1h": 0.844, "6h": 0.831, "24h": 0.819, "7d": 0.803},
    "toxicity_judge_rate":   {"1h": 0.2,   "6h": 0.3,   "24h": 0.4,   "7d": 0.6},
    "prompt_injection_rate": {"1h": 0.0,   "6h": 0.1,   "24h": 0.2,   "7d": 0.3},
    "pii_exposure_rate":     {"1h": 0.1,   "6h": 0.15,  "24h": 0.2,   "7d": 0.4},
    "retrieval_precision":   {"1h": 82.4,  "6h": 80.7,  "24h": 79.4,  "7d": 77.2},
    "context_utilisation":   {"1h": 67.5,  "6h": 65.2,  "24h": 63.8,  "7d": 61.0},
}

_CATALOG_IDS:     List[str] = [c["metric_id"] for c in CATALOG]
_VALID_SCENARIOS: set       = {"ok", "no_data", "provider_unavailable", "partial", "slow"}
_VALID_WINDOWS:   set       = {"1h", "6h", "24h", "7d"}

# ── Mutable runtime state ──────────────────────────────────────────────────────
_scenario: str = "ok"

# ── FastAPI app ────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Mock Assurance Provider",
    version="0.4.0",
    description=(
        "Drop-in mock for the RAI Assurance v0.4 API. "
        "Implements the full production contract plus /v1/_test/* endpoints "
        "for switching scenarios at runtime without restarting the service."
    ),
    docs_url="/v1/docs",
    redoc_url="/v1/redoc",
    openapi_url="/v1/openapi.json",
)

# ── Helpers ────────────────────────────────────────────────────────────────────

def _seed_value(metric_id: str, window: str) -> Optional[float]:
    windows = SEED.get(metric_id, {})
    return windows.get(window) or windows.get("1h")


def _make_metric(metric_id: str, unit: str, window: str, scen: str) -> dict:
    """Build a single MetricItem dict for the given scenario."""
    if metric_id not in _CATALOG_IDS:
        return {
            "metric_id":   metric_id,
            "value":       None,
            "unit":        "",
            "data_status": "unsupported",
            "error":       (
                f"Unknown metric_id '{metric_id}'. "
                f"Known metrics: {_CATALOG_IDS}"
            ),
        }

    base_value = _seed_value(metric_id, window)

    if scen == "no_data":
        return {
            "metric_id": metric_id, "value": None,       "unit": unit,
            "data_status": "no_data",             "error": None,
        }

    if scen == "provider_unavailable":
        return {
            "metric_id": metric_id, "value": None,       "unit": unit,
            "data_status": "provider_unavailable",
            "error": "Upstream provider returned HTTP 503",
        }

    if scen == "partial":
        idx  = _CATALOG_IDS.index(metric_id)
        half = len(CATALOG) // 2
        if idx < half:
            return {
                "metric_id": metric_id, "value": base_value, "unit": unit,
                "data_status": "ok",      "error": None,
            }
        else:
            return {
                "metric_id": metric_id, "value": None,       "unit": unit,
                "data_status": "no_data", "error": None,
            }

    # "ok" and "slow" — slow applies the delay at the request level, not per-metric
    return {
        "metric_id": metric_id, "value": base_value, "unit": unit,
        "data_status": "ok", "error": None,
    }


def _rollup(statuses: List[str]) -> str:
    """
    Envelope data_status rollup rule:
      - All statuses identical  → return that status
      - Mixed statuses          → "partial"
      - Empty list              → "no_data"
    """
    unique = set(statuses)
    if not unique:
        return "no_data"
    if len(unique) == 1:
        return unique.pop()
    return "partial"


def _maybe_sleep() -> None:
    """Block for 5 s when scenario == 'slow' (simulates upstream latency)."""
    if _scenario == "slow":
        time.sleep(5)


def _effective_scenario() -> str:
    """Return scenario to apply to metric values ('slow' uses 'ok' values + sleep)."""
    return "ok" if _scenario == "slow" else _scenario


# ── Production endpoints ───────────────────────────────────────────────────────

@app.get("/v1/healthz", summary="Liveness + provider state")
def healthz():
    """Returns platform liveness and upstream provider reachability."""
    if _scenario == "provider_unavailable":
        return {"status": "degraded", "provider_status": "unreachable"}
    return {"status": "ok", "provider_status": "reachable"}


@app.get("/v1/catalog", summary="Metric catalog")
def catalog(app_id: str = Query(..., description="Application identifier")):
    """Returns the full ordered metric catalog (8 metrics)."""
    return {"metric_count": len(CATALOG), "metrics": CATALOG}


@app.get("/v1/metrics", summary="Batch metric read")
def metrics_batch(
    app_id: str = Query(..., description="Application identifier"),
    ids:    str = Query(..., description="Comma-separated metric_id list"),
    window: str = Query("1h", description="Time window: 1h | 6h | 24h | 7d"),
):
    """Returns values for multiple metrics in a single request."""
    _maybe_sleep()
    if window not in _VALID_WINDOWS:
        return JSONResponse(status_code=400, content={
            "error": f"Unknown window '{window}'. Valid: {sorted(_VALID_WINDOWS)}"
        })

    scen    = _effective_scenario()
    id_list = [i.strip() for i in ids.split(",") if i.strip()]
    metrics = []
    for mid in id_list:
        entry = next((c for c in CATALOG if c["metric_id"] == mid), None)
        unit  = entry["unit"] if entry else ""
        metrics.append(_make_metric(mid, unit, window, scen))

    return {
        "app_id":       app_id,
        "window":       window,
        "data_status":  _rollup([m["data_status"] for m in metrics]),
        "metric_count": len(metrics),
        "metrics":      metrics,
    }


@app.get("/v1/metrics/{metric_id}", summary="Single metric read")
def metrics_single(
    metric_id: str = Path(..., description="A metric_id from the catalog"),
    app_id:    str = Query(..., description="Application identifier"),
    window:    str = Query("1h", description="Time window: 1h | 6h | 24h | 7d"),
):
    """Returns the value for a single metric."""
    _maybe_sleep()
    if window not in _VALID_WINDOWS:
        return JSONResponse(status_code=400, content={
            "error": f"Unknown window '{window}'. Valid: {sorted(_VALID_WINDOWS)}"
        })

    scen  = _effective_scenario()
    entry = next((c for c in CATALOG if c["metric_id"] == metric_id), None)
    unit  = entry["unit"] if entry else ""
    m     = _make_metric(metric_id, unit, window, scen)

    return {
        "app_id":       app_id,
        "window":       window,
        "data_status":  m["data_status"],
        "metric_count": 1,
        "metrics":      [m],
    }


# ── Test-harness endpoints (NOT part of the real Assurance v0.4 contract) ──────

@app.post(
    "/v1/_test/scenario",
    summary="Switch mock scenario at runtime  [TEST ONLY]",
    description="**Not part of the real Assurance contract.** Never call from production code.",
)
def set_scenario(
    scenario: str = Query(..., description="ok | no_data | provider_unavailable | partial | slow"),
):
    global _scenario
    if scenario not in _VALID_SCENARIOS:
        return JSONResponse(status_code=400, content={
            "error":   f"Unknown scenario '{scenario}'.",
            "valid":   sorted(_VALID_SCENARIOS),
            "current": _scenario,
        })
    _scenario = scenario
    return {"scenario": _scenario, "changed": True}


@app.get(
    "/v1/_test/scenario",
    summary="Read current scenario  [TEST ONLY]",
    description="**Not part of the real Assurance contract.** Never call from production code.",
)
def get_scenario():
    return {"scenario": _scenario}


# ── Entrypoint ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("ASSURANCE_PORT", "3010"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
