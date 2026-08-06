from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

logger = logging.getLogger(__name__)

from api.v1.models import (
    CatalogItem,
    CatalogResponse,
    HealthResponse,
    MetricItem,
    MetricsResponse,
)

_MAX_RANGE = timedelta(days=7)


def _parse_iso(value: str, name: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, f"Invalid ISO-8601 value for '{name}': {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _resolve_window(
    window: str | None,
    since: str | None,
    until: str | None,
) -> tuple[int, str]:
    """Returns (since_hours, echo_label). Validates mutual exclusion + max range."""
    if window and (since or until):
        raise HTTPException(400, "Specify either 'window' or 'since'/'until', not both")
    if since or until:
        if not since:
            raise HTTPException(400, "'since' is required when using a date range")
        since_dt = _parse_iso(since, "since")
        until_dt = _parse_iso(until, "until") if until else datetime.now(timezone.utc)
        delta = until_dt - since_dt
        if delta.total_seconds() <= 0:
            raise HTTPException(400, "'since' must be before 'until'")
        if delta > _MAX_RANGE:
            raise HTTPException(400, f"Maximum range is {_MAX_RANGE.days} days")
        return max(1, int(delta.total_seconds() / 3600)), f"{since_dt.isoformat()}__{until_dt.isoformat()}"
    chosen = window or "1h"
    if chosen not in _WINDOW_HOURS:
        raise HTTPException(400, f"Invalid window '{chosen}'. Allowed: {sorted(_WINDOW_HOURS)}")
    return _WINDOW_HOURS[chosen], chosen


# Tier-1 catalog. Keeps current metric_name values until Tier-2 namespacing lands.
# Unit values normalised to the SRS enum: %, score, count, ms.
_CATALOG: dict[str, str] = {
    # fairness — statistical disparity (rate-like, expressed as %)
    "approval_rate_disparity_by_gender":     "%",
    "denial_rate_disparity_by_gender":       "%",
    "risk_score_disparity_by_gender":        "score",
    "approval_rate_disparity_by_age":        "%",
    # fairness — Phoenix LLM-as-judge (env-gated)
    "demographic_bias_rate":                 "%",
    "decision_fairness_rate":                "%",
    # safety
    "toxicity_rate":                         "%",
    "offensive_content_rate":                "%",
    "escalation_rate":                       "%",
    "negative_sentiment_rate":               "%",
    # reliability
    "decisioning_latency_p50_ms":            "ms",
    "conversational_latency_p50_ms":         "ms",
    "decisioning_score_consistency":         "score",
    "conversational_safe_interaction_rate":  "%",
    # privacy
    "pii_detection_rate":                    "%",
    # SRS §4.7.3 mandatory — Phoenix LLM-as-judge (gated by EVAL_QUALITY / EVAL_SAFETY / EVAL_PRIVACY)
    "hallucination_rate":                    "%",
    "groundedness_score":                    "score",
    "response_relevance":                    "score",
    "toxicity_judge_rate":                   "%",
    "prompt_injection_rate":                 "%",
    "pii_exposure_rate":                     "%",
}

_WINDOW_HOURS: dict[str, int] = {"1h": 1, "6h": 6, "24h": 24, "7d": 168}

# Metrics whose underlying methodology is known-imperfect and should be reported
# with data_status="experimental" rather than "ok" so dashboards can render them
# as indicative-only.
#
# hallucination_rate, groundedness_score:
#   These run through the same Phoenix LLM-as-judge pipeline as the other 4
#   quality metrics (Claude Haiku 4.5 via _make_classifier in evals/evals.py).
#   Unlike toxicity/injection/PII/relevance — which are single-text
#   classifications the judge can answer from the text alone — hallucination
#   and groundedness require a *ground-truth premise* (e.g., retrieved RAG
#   context, source documents) to score reliably. We currently pass only
#   (user_message, response), so the judge has to fall back on plausibility,
#   which biases it toward labeling unverifiable claims as "hallucinated".
#   Result: inflated hallucination rates and depressed groundedness scores
#   on benign Q→A traffic. They graduate to "ok" once the input pairs include
#   retrieved-document context the judge can verify against.
_EXPERIMENTAL_METRICS: set[str] = {
    "hallucination_rate",
    "groundedness_score",
}

# Per-app Arize project mapping. When clients use the RAI SDK with
# OTEL_RESOURCE_ATTRIBUTES=model_id=<app_id>, their spans land in an Arize
# project that matches app_id 1:1 — so app_id IS the project name. The map
# below is kept for backward-compat with non-SDK callers that emit to a
# different project name.
_APP_TO_PROJECT: dict[str, str] = {}


def _arize_project(app_id: str) -> str:
    return _APP_TO_PROJECT.get(app_id, app_id)


router = APIRouter(prefix="/v1", tags=["v1"])


@router.get("/healthz", response_model=HealthResponse)
def healthz():
    """Liveness + provider reachability."""
    provider_status: Literal["reachable", "unreachable", "unknown"] = "unknown"
    try:
        from core.provider_registry import registry
        provider = registry.get_active()
        provider_status = "reachable" if provider.is_available() else "unreachable"
    except Exception:
        provider_status = "unreachable"
    return HealthResponse(
        status="ok" if provider_status == "reachable" else "degraded",
        provider_status=provider_status,
    )


@router.get("/catalog", response_model=CatalogResponse)
def catalog(
    app_id: str | None = Query(default=None, description="Filter to capabilities for one app (Tier 1: returns full catalog)"),
):
    items = [CatalogItem(metric_id=mid, unit=unit) for mid, unit in sorted(_CATALOG.items())]
    return CatalogResponse(metric_count=len(items), metrics=items)


def _resolve(app_id: str, requested: list[str], since_hours: int) -> tuple[list[MetricItem], str]:
    from metrics.tier2metrics import run_cycle

    try:
        produced = run_cycle(project=_arize_project(app_id), since_hours=since_hours)
        by_name = {m.metric_id: m for m in produced}
        provider_ok = True
    except Exception:
        by_name = {}
        provider_ok = False

    items: list[MetricItem] = []
    for mid in requested:
        if mid not in _CATALOG:
            items.append(MetricItem(metric_id=mid, value=None, unit="", data_status="unsupported", error=f"Unknown metric_id '{mid}'"))
            continue
        unit = _CATALOG[mid]
        if not provider_ok:
            items.append(MetricItem(metric_id=mid, value=None, unit=unit, data_status="provider_unavailable", error="Upstream provider unreachable"))
            continue
        m = by_name.get(mid)
        if m is None or m.span_count == 0:
            items.append(MetricItem(metric_id=mid, value=None, unit=unit, data_status="no_data"))
            continue
        status = "experimental" if mid in _EXPERIMENTAL_METRICS else "ok"
        items.append(MetricItem(metric_id=mid, value=m.value, unit=unit, data_status=status))

    # ─── LLM-cache override (pure LLM, no fast fallback) ──────────────────
    # When METRICS_USE_LLM_CACHE=true, the 6 LLM-judge metric IDs are sourced
    # EXCLUSIVELY from the in-memory LLM-evals cache. No fast-path fallback —
    # if the cache is empty for a given (app, window) key, those 6 metrics
    # return data_status="no_data" until the background scheduler populates
    # the cache (~30 min cadence by default).
    #
    # Rationale: customer doesn't want hybrid (LLM + local ML) values mixed.
    # Either we have LLM-judge ground truth, or we don't show a number.
    if os.getenv("METRICS_USE_LLM_CACHE", "false").lower() in ("1", "true", "yes", "on"):
        cached = _LLM_EVAL_CACHE.get((app_id, since_hours))
        cached_by_id = {it.metric_id: it for it in cached["response"].metrics} if cached else {}
        for i, item in enumerate(items):
            if item.metric_id in _LLM_EVAL_IDS:
                if item.metric_id in cached_by_id:
                    items[i] = cached_by_id[item.metric_id]
                else:
                    # Cache miss for an LLM-judge metric — return no_data
                    # rather than the fast-path value (no hybrid).
                    items[i] = MetricItem(
                        metric_id=item.metric_id,
                        value=None,
                        unit=item.unit,
                        data_status="no_data",
                        error="LLM eval cache not yet populated for this window",
                    )

    statuses = {it.data_status for it in items}
    if statuses == {"provider_unavailable"}:
        envelope = "provider_unavailable"
    elif statuses == {"no_data"}:
        envelope = "no_data"
    elif statuses == {"unsupported"}:
        envelope = "unsupported"
    else:
        envelope = "ok"
    return items, envelope


@router.get("/metrics", response_model=MetricsResponse)
def metrics(
    app_id: str = Query(..., description="Application identifier (1:1 with Arize project name per D1)"),
    ids: str | None = Query(default=None, description="Comma-separated metric IDs. Omit for full catalog."),
    window: str | None = Query(default=None, description="Window preset: 1h | 6h | 24h | 7d (mutually exclusive with since/until)"),
    since: str | None = Query(default=None, description="ISO-8601 UTC start. Mutually exclusive with window. Max range 7 days."),
    until: str | None = Query(default=None, description="ISO-8601 UTC end. Defaults to now if omitted."),
):
    """Read one or more metrics for a single app + window."""
    since_hours, label = _resolve_window(window, since, until)

    requested = [s.strip() for s in ids.split(",") if s.strip()] if ids else list(_CATALOG.keys())
    if not requested:
        raise HTTPException(400, "Empty 'ids' parameter")

    items, envelope = _resolve(app_id, requested, since_hours)
    return MetricsResponse(
        app_id=app_id,
        window=label,
        data_status=envelope,  # type: ignore[arg-type]
        metric_count=len(items),
        metrics=items,
    )


# IDs returned by run_llm_evals_window — must match _CATALOG entries
_LLM_EVAL_IDS = {
    "demographic_bias_rate",
    "decision_fairness_rate",
    "refusal_rate",
    "correctness_rate",
    "hallucination_rate",
    "groundedness_score",
    "response_relevance",
    "toxicity_judge_rate",
    "prompt_injection_rate",
    "pii_exposure_rate",
}


def _compute_llm_evals_response(
    app_id: str,
    requested: list[str],
    since_hours: int,
    label: str,
) -> MetricsResponse:
    """Run the Phoenix LLM-as-judge evaluators and return a MetricsResponse.

    Extracted so both the on-demand /llm-evals endpoint and the background
    cache warmer in api/main.py can call the same code path.
    """
    from evals.evals import run_llm_evals_window

    provider_error = ""
    try:
        result = run_llm_evals_window(since_hours=since_hours, project=_arize_project(app_id))
        produced = result["metrics"]
        provider_ok = True
    except Exception as e:
        produced = {}
        provider_ok = False
        provider_error = str(e)[:200]

    items: list[MetricItem] = []
    for mid in requested:
        unit = _CATALOG.get(mid, "%")
        if not provider_ok:
            items.append(MetricItem(metric_id=mid, value=None, unit=unit, data_status="provider_unavailable", error=provider_error))
            continue
        m = produced.get(mid)
        if m is None or m.span_count == 0:
            items.append(MetricItem(metric_id=mid, value=None, unit=unit, data_status="no_data"))
            continue
        status = "experimental" if mid in _EXPERIMENTAL_METRICS else "ok"
        items.append(MetricItem(metric_id=mid, value=m.value, unit=unit, data_status=status))

    statuses = {it.data_status for it in items}
    if statuses == {"provider_unavailable"}:
        envelope = "provider_unavailable"
    elif statuses == {"no_data"}:
        envelope = "no_data"
    else:
        envelope = "ok"

    return MetricsResponse(
        app_id=app_id,
        window=label,
        data_status=envelope,  # type: ignore[arg-type]
        metric_count=len(items),
        metrics=items,
    )


@router.get("/llm-evals", response_model=MetricsResponse, tags=["v1"])
def llm_evals(
    app_id: str = Query(..., description="Application identifier (1:1 with Arize project name)"),
    ids: str | None = Query(default=None, description="Comma-separated LLM-as-judge metric IDs. Omit for all 10."),
    window: str | None = Query(default=None, description="Window preset: 1h | 6h | 24h | 7d"),
    since: str | None = Query(default=None, description="ISO-8601 UTC start"),
    until: str | None = Query(default=None, description="ISO-8601 UTC end"),
):
    """Run the 10 Phoenix LLM-as-judge evaluators. Slow (30-90s) — call on demand."""
    since_hours, label = _resolve_window(window, since, until)
    requested = [s.strip() for s in ids.split(",") if s.strip()] if ids else sorted(_LLM_EVAL_IDS)

    invalid = [r for r in requested if r not in _LLM_EVAL_IDS]
    if invalid:
        raise HTTPException(400, f"Not LLM-as-judge metric IDs: {invalid}. Allowed: {sorted(_LLM_EVAL_IDS)}")

    return _compute_llm_evals_response(app_id, requested, since_hours, label)


# ─── Cached LLM-evals — for dashboard polling without the 30-90s wait ────────
#
# In-memory cache keyed by (app_id, since_hours). Populated by:
#   • Background scheduler (api/main.py, every LLM_EVAL_CACHE_REFRESH_MIN minutes)
#   • Lazy fallback on cache miss (first poll triggers a sync compute)
#
# The response shape matches /llm-evals exactly so consumers can swap endpoints
# transparently. Cache freshness is reported via the response header
# X-Cache-Age-Seconds (added by FastAPI middleware? — for now, just-in-time
# poll behaviour is: cache miss = sync compute + store; cache hit = return cache).

_LLM_EVAL_CACHE: dict[tuple[str, int], dict] = {}
_LLM_EVAL_CACHE_LOCK = threading.Lock()

# Apps to pre-warm in the cache. Background scheduler iterates over these
# (cross product with windows below). Override via env LLM_EVAL_CACHE_APPS.
_CACHE_APPS = [a.strip() for a in os.getenv("LLM_EVAL_CACHE_APPS", "mybank,loanapp").split(",") if a.strip()]
_CACHE_WINDOWS = [w.strip() for w in os.getenv("LLM_EVAL_CACHE_WINDOWS", "1h,24h").split(",") if w.strip()]


def refresh_llm_eval_cache_entry(app_id: str, window: str) -> None:
    """Compute fresh LLM-evals for (app, window) and store under the cache key.

    Called by the background scheduler. Makes an HTTP self-call to
    /v1/llm-evals rather than invoking Phoenix in-thread — Phoenix's async
    LLM calls were failing with APIConnectionError when invoked from
    BackgroundScheduler worker threads on macOS. Routing through the FastAPI
    event loop (via localhost HTTP) sidesteps the threading issue entirely.
    """
    import httpx

    since_hours, label = _resolve_window(window, None, None)
    port = int(os.getenv("PORT", os.getenv("ASSURANCE_PORT", "3010")))
    url = f"http://127.0.0.1:{port}/v1/llm-evals"
    params = {"app_id": app_id, "window": window}

    try:
        with httpx.Client(timeout=300.0) as client:
            r = client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        logger.error(f"[LLMEvalCache] refresh self-call failed for {app_id}/{window}: {exc}")
        return

    # Reconstruct MetricsResponse from the JSON payload so the cache holds a
    # typed object (matches the in-thread code path's return shape).
    response = MetricsResponse(
        app_id=data["app_id"],
        window=data["window"],
        data_status=data["data_status"],
        metric_count=data["metric_count"],
        metrics=[MetricItem(**m) for m in data["metrics"]],
    )

    with _LLM_EVAL_CACHE_LOCK:
        _LLM_EVAL_CACHE[(app_id, since_hours)] = {
            "computed_at": datetime.now(timezone.utc),
            "response": response,
        }
    logger.info(f"[LLMEvalCache] refreshed {app_id} window={label} envelope={response.data_status} count={response.metric_count}")


@router.get("/llm-evals-cached", response_model=MetricsResponse, tags=["v1"])
def llm_evals_cached(
    app_id: str = Query(..., description="Application identifier"),
    window: str | None = Query(default="1h", description="Window preset: 1h | 6h | 24h | 7d"),
):
    """Return the most recent cached LLM-evals result.

    Designed for the PAP dashboard's T2 panel to poll without the 30-90s wait
    of /llm-evals. The cache is kept warm by a background scheduler running
    every LLM_EVAL_CACHE_REFRESH_MIN minutes (default 15).

    Behaviour:
      • Cache hit  → return cached response immediately (sub-millisecond).
      • Cache miss → trigger a synchronous compute, populate the cache, return.
                      First poll after restart will block 30-90s; subsequent
                      polls return instantly.
    """
    since_hours, label = _resolve_window(window, None, None)
    key = (app_id, since_hours)

    cached = _LLM_EVAL_CACHE.get(key)
    if cached is not None:
        return cached["response"]

    # Cold-cache fallback — compute synchronously and cache for next poll.
    logger.info(f"[LLMEvalCache] cold miss for {app_id} window={label} — computing inline")
    refresh_llm_eval_cache_entry(app_id, window or "1h")
    return _LLM_EVAL_CACHE[key]["response"]


@router.get("/llm-evals-cached/status", tags=["v1"], include_in_schema=False)
def llm_evals_cache_status():
    """Diagnostic — show what's currently in the LLM-eval cache."""
    out = []
    with _LLM_EVAL_CACHE_LOCK:
        for (app_id, since_hours), entry in _LLM_EVAL_CACHE.items():
            age_sec = (datetime.now(timezone.utc) - entry["computed_at"]).total_seconds()
            out.append({
                "app_id":             app_id,
                "since_hours":        since_hours,
                "computed_at":        entry["computed_at"].isoformat(),
                "cache_age_seconds":  round(age_sec, 1),
                "envelope":           entry["response"].data_status,
                "metric_count":       entry["response"].metric_count,
            })
    return {
        "entries":         out,
        "configured_apps": _CACHE_APPS,
        "configured_windows": _CACHE_WINDOWS,
        "refresh_minutes": int(os.getenv("LLM_EVAL_CACHE_REFRESH_MIN", "15")),
    }


@router.get("/demo/llm-evals", response_class=HTMLResponse, include_in_schema=False)
def llm_evals_demo_page() -> str:
    """Self-contained HTML demo page with a 'Run LLM Evals' button.

    Calls the same /v1/llm-evals endpoint and renders results in a styled
    table. No framework — just vanilla JS + fetch. Designed for live demos.
    """
    return _LLM_EVALS_DEMO_HTML


@router.get("/metrics/{metric_id}", response_model=MetricsResponse)
def metric_by_id(
    metric_id: str,
    app_id: str = Query(..., description="Application identifier"),
    window: str | None = Query(default=None, description="Window preset: 1h | 6h | 24h | 7d"),
    since: str | None = Query(default=None, description="ISO-8601 UTC start"),
    until: str | None = Query(default=None, description="ISO-8601 UTC end"),
):
    """Convenience variant — same envelope shape, single metric."""
    since_hours, label = _resolve_window(window, since, until)
    items, envelope = _resolve(app_id, [metric_id], since_hours)
    return MetricsResponse(
        app_id=app_id,
        window=label,
        data_status=envelope,  # type: ignore[arg-type]
        metric_count=len(items),
        metrics=items,
    )


# ─── LLM Evals Demo Page (HTML) ──────────────────────────────────────────────

_LLM_EVALS_DEMO_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>RAI Assurance — LLM Evals Demo</title>
<style>
  :root {
    --bg: #0a0f1c;
    --panel: #111827;
    --border: #1f2937;
    --text: #c8d0e0;
    --muted: #6b7a8e;
    --accent: #4ade80;
    --warn: #facc15;
    --err: #f87171;
    --info: #60a5fa;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: var(--bg); color: var(--text);
    min-height: 100vh;
  }
  h1 {
    margin: 0 0 4px; font-size: 1.4rem; font-weight: 600;
    color: var(--text);
  }
  .subtitle { color: var(--muted); font-size: 0.85rem; margin-bottom: 24px; }
  .panel {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 8px; padding: 20px; margin-bottom: 16px;
  }
  .form-row {
    display: flex; gap: 16px; align-items: end; flex-wrap: wrap;
    margin-bottom: 12px;
  }
  .form-field { display: flex; flex-direction: column; gap: 4px; }
  .form-field label {
    font-size: 0.7rem; text-transform: uppercase; color: var(--muted);
    letter-spacing: 0.05em;
  }
  .form-field select, .form-field input {
    background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 4px;
    padding: 8px 10px; font-size: 0.9rem; min-width: 140px;
    font-family: inherit;
  }
  button {
    background: var(--accent); color: #0a0f1c;
    border: none; border-radius: 4px; padding: 10px 18px;
    font-size: 0.9rem; font-weight: 600; cursor: pointer;
    transition: opacity 0.15s;
  }
  button:hover { opacity: 0.85; }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  .warn { color: var(--warn); font-size: 0.8rem; margin-top: 8px; }
  .status {
    font-size: 0.85rem; color: var(--muted); margin: 12px 0;
    min-height: 1.2em;
  }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th, td {
    text-align: left; padding: 8px 12px;
    border-bottom: 1px solid var(--border);
  }
  th {
    color: var(--muted); font-weight: 500;
    text-transform: uppercase; font-size: 0.7rem;
    letter-spacing: 0.05em;
  }
  td.metric-id { font-family: 'SF Mono', Monaco, monospace; }
  td.value { font-weight: 600; }
  .badge {
    display: inline-block; padding: 2px 8px;
    border-radius: 3px; font-size: 0.7rem; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.04em;
  }
  .badge.ok { background: rgba(74,222,128,0.15); color: var(--accent); }
  .badge.experimental { background: rgba(250,204,21,0.15); color: var(--warn); }
  .badge.no_data { background: rgba(107,122,142,0.15); color: var(--muted); }
  .badge.provider_unavailable, .badge.unsupported {
    background: rgba(248,113,113,0.15); color: var(--err);
  }
  .spinner {
    display: inline-block; width: 12px; height: 12px;
    border: 2px solid var(--border); border-top-color: var(--info);
    border-radius: 50%; animation: spin 0.8s linear infinite;
    vertical-align: middle; margin-right: 8px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .envelope-row {
    display: flex; gap: 24px; padding: 12px 16px;
    background: rgba(96,165,250,0.05); border-radius: 4px;
    margin-bottom: 12px; font-size: 0.85rem;
  }
  .envelope-row span { color: var(--muted); }
  .envelope-row b { color: var(--text); margin-left: 4px; }
</style>
</head>
<body>

<h1>RAI Assurance — LLM-as-Judge Evals</h1>
<div class="subtitle">
  On-demand Phoenix LLM evaluators &middot; same Tier 2 metric IDs as <code>/v1/metrics</code>,
  but scored by a prompted LLM judge instead of local ML models.
</div>

<div class="panel">
  <div class="form-row">
    <div class="form-field">
      <label for="app">App</label>
      <select id="app">
        <option value="loanapp">loanapp (LoanWise)</option>
        <option value="mybank">mybank (MyBank)</option>
      </select>
    </div>
    <div class="form-field">
      <label for="window">Window</label>
      <select id="window">
        <option value="1h">last 1 hour</option>
        <option value="6h">last 6 hours</option>
        <option value="24h" selected>last 24 hours</option>
        <option value="7d">last 7 days</option>
      </select>
    </div>
    <div class="form-field">
      <label for="ids">Metric IDs (optional)</label>
      <input id="ids" type="text" placeholder="all 10 if blank" style="min-width:260px;">
    </div>
    <button id="run">Run LLM Evals</button>
  </div>
  <div class="warn">&#9888; Takes 30&ndash;90 seconds &mdash; Phoenix calls a judge LLM per span. Don't refresh while running.</div>
</div>

<div class="status" id="status">Pick app + window, then click <b>Run LLM Evals</b>.</div>

<div class="panel" id="results-panel" style="display:none;">
  <div class="envelope-row">
    <span>envelope: <b id="env-status">&mdash;</b></span>
    <span>app: <b id="env-app">&mdash;</b></span>
    <span>window: <b id="env-window">&mdash;</b></span>
    <span>metric_count: <b id="env-count">&mdash;</b></span>
  </div>
  <table>
    <thead><tr><th>Metric</th><th>Value</th><th>Unit</th><th>Status</th><th>Error</th></tr></thead>
    <tbody id="results-tbody"></tbody>
  </table>
</div>

<script>
const runBtn = document.getElementById('run');
const statusEl = document.getElementById('status');
const resultsPanel = document.getElementById('results-panel');
const tbody = document.getElementById('results-tbody');

runBtn.addEventListener('click', async () => {
  const app = document.getElementById('app').value;
  const win = document.getElementById('window').value;
  const ids = document.getElementById('ids').value.trim();

  const url = new URL(window.location.origin + '/v1/llm-evals');
  url.searchParams.set('app_id', app);
  url.searchParams.set('window', win);
  if (ids) url.searchParams.set('ids', ids);

  runBtn.disabled = true;
  resultsPanel.style.display = 'none';
  const startedAt = Date.now();
  statusEl.innerHTML = '<span class="spinner"></span> Running LLM judge evaluators against <b>' + app + '</b> ... this may take 30&ndash;90 sec';

  try {
    const resp = await fetch(url.toString());
    if (!resp.ok) {
      const errText = await resp.text();
      throw new Error('HTTP ' + resp.status + ' \\u2014 ' + errText);
    }
    const data = await resp.json();
    const elapsed = ((Date.now() - startedAt) / 1000).toFixed(1);
    statusEl.innerHTML = '\\u2713 Done in <b>' + elapsed + ' sec</b>. ' + data.metric_count + ' metric(s) returned.';

    document.getElementById('env-status').textContent = data.data_status;
    document.getElementById('env-app').textContent = data.app_id;
    document.getElementById('env-window').textContent = data.window;
    document.getElementById('env-count').textContent = data.metric_count;

    tbody.innerHTML = '';
    data.metrics.forEach(m => {
      const tr = document.createElement('tr');
      const val = (m.value === null || m.value === undefined)
        ? '\\u2014'
        : (typeof m.value === 'number' ? m.value.toFixed(3) : m.value);
      tr.innerHTML = `
        <td class="metric-id">${m.metric_id}</td>
        <td class="value">${val}</td>
        <td>${m.unit || ''}</td>
        <td><span class="badge ${m.data_status}">${m.data_status}</span></td>
        <td>${m.error ? '<span style="color:var(--err);">' + m.error + '</span>' : ''}</td>
      `;
      tbody.appendChild(tr);
    });
    resultsPanel.style.display = 'block';
  } catch (err) {
    statusEl.innerHTML = '<span style="color:var(--err);">\\u2717 ' + err.message + '</span>';
  } finally {
    runBtn.disabled = false;
  }
});
</script>

</body>
</html>
"""
