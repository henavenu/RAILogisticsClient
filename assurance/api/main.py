from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

# Live pillar API (/metrics/summary) serves only fast statistical metrics.
# LLM-as-judge evals run on demand via /llm-evals (see run_llm_evals_window).
os.environ.setdefault("EVAL_FAIRNESS", "false")
os.environ.setdefault("EVAL_SAFETY", "false")
os.environ.setdefault("EVAL_RELIABILITY", "false")
os.environ.setdefault("EVAL_QUALITY", "false")
os.environ.setdefault("EVAL_PRIVACY", "false")

from metrics.tier2metrics import run_cycle

_PILLARS: dict[str, list[str]] = {
    "fairness": [
        "approval_rate_disparity_by_gender",
        "denial_rate_disparity_by_gender",
        "risk_score_disparity_by_gender",
        "approval_rate_disparity_by_age",
    ],
    "safety": [
        "toxicity_rate",
        "offensive_content_rate",
        "escalation_rate",
        "negative_sentiment_rate",
    ],
    "reliability": [
        "decisioning_latency_p50_ms",
        "conversational_latency_p50_ms",
        "decisioning_score_consistency",
        "conversational_safe_interaction_rate",
    ],
    "privacy": [
        "pii_detection_rate",
    ],
}

_VALID_METRIC_TYPES = set(_PILLARS.keys())


class MetricDetail(BaseModel):
    metric_id: str
    value: float
    unit: str
    span_count: int
    breakdown: dict


class PillarSummary(BaseModel):
    metric_count: int
    metrics: list[str]
    details: dict[str, MetricDetail]


class SummaryResponse(BaseModel):
    pillars: dict[str, PillarSummary]
    total_metrics: int
    window_hours: int
    as_of: datetime


class RunResponse(BaseModel):
    metrics_computed: int
    completed_at: datetime


app = FastAPI(
    title="RAI Assurance Service",
    description="Observability & Assurance API for the RAI Trust Platform.",
    version="0.5.0",
    openapi_url="/v1/openapi.json",
    docs_url="/v1/docs",
    redoc_url="/v1/redoc",
)

from api.v1.router import router as v1_router

app.include_router(v1_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000", "http://127.0.0.1:3000",
        "http://localhost:3002", "http://127.0.0.1:3002",
        "http://localhost:3003", "http://127.0.0.1:3003",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ─── LLM-Evals background cache warmer ───────────────────────────────────────
#
# Refreshes /v1/llm-evals-cached for the configured (app, window) pairs every
# LLM_EVAL_CACHE_REFRESH_MIN minutes. Default 15 min — tune via env.
#
# Disable entirely with LLM_EVAL_CACHE_ENABLED=false (useful for unit tests
# or environments where you only want lazy-on-demand caching).
import logging as _logging
import os as _os
_main_logger = _logging.getLogger("api.main")


@app.on_event("startup")
def _start_llm_eval_cache_warmer() -> None:
    if _os.getenv("LLM_EVAL_CACHE_ENABLED", "true").lower() not in ("1", "true", "yes", "on"):
        _main_logger.info("[LLMEvalCache] disabled via LLM_EVAL_CACHE_ENABLED=false")
        return

    refresh_min = int(_os.getenv("LLM_EVAL_CACHE_REFRESH_MIN", "15"))

    from apscheduler.schedulers.background import BackgroundScheduler
    from api.v1.router import (
        refresh_llm_eval_cache_entry,
        _CACHE_APPS,
        _CACHE_WINDOWS,
    )

    def _refresh_all():
        for app_id in _CACHE_APPS:
            for window in _CACHE_WINDOWS:
                try:
                    refresh_llm_eval_cache_entry(app_id, window)
                except Exception as exc:
                    _main_logger.error(f"[LLMEvalCache] refresh failed for {app_id}/{window}: {exc}")

    scheduler = BackgroundScheduler(daemon=True)
    # First refresh runs ~1 min after startup (gives services time to settle),
    # then every refresh_min minutes thereafter.
    scheduler.add_job(_refresh_all, "interval", minutes=refresh_min, next_run_time=datetime.now() + timedelta(minutes=1))
    scheduler.start()
    _main_logger.info(
        f"[LLMEvalCache] background warmer started — apps={_CACHE_APPS} windows={_CACHE_WINDOWS} every={refresh_min}min"
    )


@app.get("/healthz", tags=["ops"])
def healthz():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/metrics/summary", tags=["metrics"], response_model=SummaryResponse)
def metrics_summary(
    window: int = Query(default=1, ge=1, le=168, description="Time window in hours (default: 1)"),
    metric_type: str | None = Query(default=None, description="Filter by pillar: fairness | safety | reliability | privacy"),
):
    if metric_type and metric_type not in _VALID_METRIC_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid metric_type '{metric_type}'. Valid: {sorted(_VALID_METRIC_TYPES)}")

    results = run_cycle(since_hours=window)
    result_map = {r.metric_id: r for r in results}

    pillars_to_show = {metric_type: _PILLARS[metric_type]} if metric_type else _PILLARS

    pillar_summaries: dict[str, PillarSummary] = {}
    total_metrics = 0

    for pillar, names in pillars_to_show.items():
        in_pillar = [result_map[n] for n in names if n in result_map]
        pillar_summaries[pillar] = PillarSummary(
            metric_count=len(in_pillar),
            metrics=names,
            details={
                m.metric_id: MetricDetail(
                    metric_id=m.metric_id,
                    value=m.value,
                    unit=m.unit,
                    span_count=m.span_count,
                    breakdown=m.breakdown,
                )
                for m in in_pillar
            },
        )
        total_metrics += len(in_pillar)

    return SummaryResponse(
        pillars=pillar_summaries,
        total_metrics=total_metrics,
        window_hours=window,
        as_of=datetime.now(timezone.utc),
    )


@app.get("/metrics", tags=["metrics"])
def metrics_list(
    window: int = Query(default=1, ge=1, le=168, description="Time window in hours (default: 1)"),
    metric_type: str | None = Query(default=None, description="Filter by pillar: fairness | safety | reliability | privacy"),
):
    if metric_type and metric_type not in _VALID_METRIC_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid metric_type '{metric_type}'. Valid: {sorted(_VALID_METRIC_TYPES)}")

    results = run_cycle(since_hours=window)

    if metric_type:
        names   = set(_PILLARS[metric_type])
        results = [r for r in results if r.metric_id in names]

    if not results:
        return {"metrics": [], "count": 0}

    return {"metrics": [r.model_dump() for r in results], "count": len(results)}


@app.get("/metrics/{name}", tags=["metrics"])
def metric_by_name(
    name: str,
    window: int = Query(default=1, ge=1, le=168, description="Time window in hours (default: 1)"),
):
    results    = run_cycle(since_hours=window)
    result_map = {r.metric_id: r for r in results}
    if name not in result_map:
        raise HTTPException(status_code=404, detail=f"Metric '{name}' not found.")
    return result_map[name].model_dump()


@app.post("/jobs/run", tags=["jobs"], response_model=RunResponse)
def jobs_run(since_hours: int = 1):
    results = run_cycle(since_hours=since_hours)
    return RunResponse(
        metrics_computed=len(results),
        completed_at=datetime.now(timezone.utc),
    )


@app.get("/agents", tags=["agents"])
def agents_list():
    """List available bias-detection providers and the active default."""
    from core.agents.registry import bias_agents

    return {
        "active": os.getenv("BIAS_AGENT_PROVIDER", "mock"),
        "available": bias_agents.available(),
    }


class BiasDetectionRequest(BaseModel):
    data: list[dict]
    outcome_col: str = "decision"
    favorable_outcome: str = "Approved"
    provider: str | None = None


@app.post("/agents/bias-detection/invoke", tags=["agents"])
def agents_bias_detection_invoke(req: BiasDetectionRequest):
    """Run the bias-detection agent on caller-supplied records. Bypasses Arize fetch."""
    from core.agents.registry import bias_agents

    try:
        agent = bias_agents.get(req.provider) if req.provider else bias_agents.get_active()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not agent.is_available():
        raise HTTPException(status_code=503, detail=f"Bias agent '{agent.name}' is not reachable.")

    try:
        report = agent.detect(req.data, outcome_col=req.outcome_col, favorable_outcome=req.favorable_outcome)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Bias agent call failed: {e}")
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {
        "provider": agent.name,
        "records_analyzed": report.records_analyzed,
        "report": report.model_dump(),
    }


@app.get("/agents/bias-detection", tags=["agents"])
def agents_bias_detection(
    window: int = Query(default=1, ge=1, le=168, description="Time window in hours (default: 1)"),
    provider: str | None = Query(default=None, description="Override provider: mock"),
    project: str = Query(default="rai-assurance", description="Arize project name"),
):
    """Pull loan_decision spans from Arize, adapt to records, and run the bias-detection agent."""
    from core.agents.registry import bias_agents
    from core.agents.span_adapter import spans_to_records
    from core.provider_registry import registry

    try:
        agent = bias_agents.get(provider) if provider else bias_agents.get_active()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not agent.is_available():
        raise HTTPException(
            status_code=503,
            detail=f"Bias agent '{agent.name}' is not reachable. Check the service is running and configured.",
        )

    span_provider = registry.get_active()
    loan_spans = span_provider.fetch_spans(project, "loan_decision", since_hours=window)
    records = spans_to_records(loan_spans)

    if not records:
        return {
            "provider": agent.name,
            "window_hours": window,
            "span_count": len(loan_spans),
            "records_analyzed": 0,
            "report": None,
            "note": "No loan_decision records with a usable decision attribute in this window.",
        }

    try:
        report = agent.detect(records)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Bias agent call failed: {e}")
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {
        "provider": agent.name,
        "window_hours": window,
        "span_count": len(loan_spans),
        "records_analyzed": report.records_analyzed,
        "report": report.model_dump(),
    }


@app.get("/llm-evals", tags=["llm-evals"])
def llm_evals(
    window: int = Query(default=1, ge=1, le=168, description="Time window in hours (default: 1)"),
):
    """Run all 4 Phoenix LLM-as-judge evaluators in batch and return aggregated rates
    plus per-span judgments with explanations. Slower than /metrics/summary — call on demand."""
    from evals.evals import run_llm_evals_window

    result = run_llm_evals_window(since_hours=window)
    return {
        "window_hours":       result["window_hours"],
        "project":            result["project"],
        "support_span_count": result["support_span_count"],
        "loan_span_count":    result["loan_span_count"],
        "metrics":   {name: m.model_dump() for name, m in result["metrics"].items()},
        "judgments": result["judgments"],
        "as_of":     datetime.now(timezone.utc),
    }
