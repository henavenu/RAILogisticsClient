"""
Analytics API — dimension-level drill-down for the governance dashboard.

Drill path (SRS §4.7 — external risk governance integration):
  App → Dimension Tiles → Evaluations → Evaluation Trail

Endpoints
---------
GET /v1/analytics/apps/{app_id}/dimensions
  Per-dimension aggregates for an app with a 7-day daily violation trend.
  Feeds the dimension tile grid when an app is selected from the external
  risk governance platform.  Mirrors the dim_scores structure already
  returned by /v1/metrics/tier1 but scoped to a single app.

GET /v1/analytics/apps/{app_id}/dimensions/{dim_id}/evaluations
  Evaluation list for a specific app + dimension, optionally:
    • filtered to violations-only (violations_only=true)
    • grouped by agent_id | content_role | use_case | function_name | none
    • scoped to a rolling day window (days=7, max=90)
  Also returns a per-day trend series (identical shape to the tile endpoint).

GET /v1/analytics/evaluations/{interaction_id}
  Full evaluation detail for one interaction: verdict, all dimension scores,
  content_snippet (if stored), pack_trace, arize_trace_url (if linked).
  Used by the Evaluation Trail panel.

Design notes
------------
• No new database required — all data comes from audit_evaluation_audit and
  audit_evaluation_dimensions (existing tables; columns added by migration 0011).
• Uses audit_db.load_audit_records() so the same field normalisation logic
  (violations JSON parsing, metadata_json → dict, timezone handling) is shared.
• group_by values map to record fields:
    agent_id       → record["agent_id"]
    content_role   → record["role"]
    use_case       → record["metadata"]["use_case"] (SDK-supplied)
    function_name  → record["function_name"]  (extracted from metadata_json)
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from core.database import SessionLocal
from api.audit_db  import load_audit_records

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/analytics", tags=["analytics"])


# ── LLM provider pick — mirrors sentinel agent pattern ────────────────────────
# Priority: Anthropic → Groq → Ollama (phi).  Each factory returns an async
# callable (system: str, user: str) -> str, or None if unavailable.

def _try_anthropic(model: str = "claude-3-5-haiku-20241022", max_tokens: int = 300):
    """
    Anthropic provider using tool use — the model is forced to populate the
    three-field schema; free-text responses are impossible at the protocol level.
    Returns a JSON string: {"trend": "...", "agents": "...", "roles": "..."}.
    """
    try:
        import anthropic
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            return None, None
        client = anthropic.AsyncAnthropic(api_key=key)
        logger.info("[explain] LLM provider: Anthropic/%s (tool use — structured output)", model)

        # Schema enforced by the API — model cannot deviate from this shape
        _EXPLAIN_TOOL = {
            "name": "report_analysis",
            "description": (
                "Report a factual pattern analysis of guardrail violations. "
                "Each field is one sentence under 25 words. "
                "Describe only what the data shows — no speculation, no recommendations."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "trend": {
                        "type": "string",
                        "description": (
                            "One sentence describing the shape of the daily violation "
                            "trend: rising, falling, spike, or flat."
                        ),
                    },
                    "agents": {
                        "type": "string",
                        "description": (
                            "One sentence stating which agents carry violations "
                            "and at what rate — factual counts only."
                        ),
                    },
                    "roles": {
                        "type": "string",
                        "description": (
                            "One sentence stating how violations distribute across "
                            "content roles — factual counts only."
                        ),
                    },
                },
                "required": ["trend", "agents", "roles"],
            },
        }

        async def call(system: str, user: str) -> str:
            resp = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                tools=[_EXPLAIN_TOOL],
                tool_choice={"type": "tool", "name": "report_analysis"},
                messages=[{"role": "user", "content": user}],
            )
            for block in resp.content:
                if block.type == "tool_use" and block.name == "report_analysis":
                    return json.dumps(block.input)   # always valid JSON
            raise ValueError("Anthropic response missing expected tool_use block")

        return call, f"anthropic/{model}"
    except ImportError:
        return None, None


def _try_groq(model: str = "llama-3.1-8b-instant", max_tokens: int = 300):
    """
    Groq provider using JSON-object response mode — instructs the model to
    return {"trend": "...", "agents": "...", "roles": "..."} and enforces
    JSON at the API level (parse errors fall through to rule-based).
    """
    try:
        from groq import AsyncGroq
        key = os.getenv("GROQ_API_KEY", "").strip()
        if not key:
            return None, None
        client = AsyncGroq(api_key=key)
        logger.info("[explain] LLM provider: Groq/%s (JSON mode)", model)

        async def call(system: str, user: str) -> str:
            resp = await client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
            )
            return resp.choices[0].message.content.strip()

        return call, f"groq/{model}"
    except ImportError:
        return None, None


def _try_ollama(
    base_url: str = "",
    model:    str = "phi3",
    max_tokens: int = 220,
):
    url = (base_url or os.getenv("OLLAMA_BASE_URL", "")).rstrip("/")
    mdl = model or os.getenv("OLLAMA_MODEL", "phi3")
    if not url:
        return None, None
    logger.info("[explain] LLM provider: Ollama (%s @ %s)", mdl, url)

    async def call(system: str, user: str) -> str:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{url}/v1/chat/completions",
                json={
                    "model": mdl, "max_tokens": max_tokens,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user",   "content": user}],
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()

    return call, f"ollama/{mdl}"


# Module-level cached provider (picked once at first call)
_llm_call   = None   # async callable or None
_llm_label  = None   # e.g. "anthropic/claude-3-5-haiku-20241022"
_llm_probed = False  # set True after first probe


def _get_llm() -> tuple:
    """Return (async_callable_or_None, label_or_None) for the best available provider."""
    global _llm_call, _llm_label, _llm_probed
    if _llm_probed:
        return _llm_call, _llm_label
    _llm_probed = True
    for factory in (_try_anthropic, _try_groq, _try_ollama):
        fn, lbl = factory()
        if fn is not None:
            _llm_call, _llm_label = fn, lbl
            return _llm_call, _llm_label
    logger.info("[explain] No LLM provider available — rule-based explain active")
    return None, None


# ── DB dependency ─────────────────────────────────────────────────────────────

def get_db():
    with SessionLocal() as db:
        yield db


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_violation(record: dict) -> bool:
    """True when the evaluation has at least one non-PASS dimension."""
    return any(
        dr.get("status") in ("WARNING", "FLAGGED")
        for dr in record.get("dimension_results", [])
    )


def _dim_violation(record: dict, dim_id: str) -> bool:
    """True when the given dimension is WARNING or FLAGGED in this record."""
    for dr in record.get("dimension_results", []):
        if dr.get("dimension") == dim_id and dr.get("status") in ("WARNING", "FLAGGED"):
            return True
    return False


def _dim_score(record: dict, dim_id: str) -> Optional[int]:
    for dr in record.get("dimension_results", []):
        if dr.get("dimension") == dim_id:
            return dr.get("score")
    return None


def _group_key(record: dict, group_by: str) -> str:
    """Extract the grouping key from a record; falls back to 'unknown'.

    For agent_id grouping, prefers metadata.agent_name (the specific pipeline
    node name set by the SDK) over the raw agent_id column, consistent with
    the normalization done in the evals_out list and the explain endpoint.
    """
    if group_by == "agent_id":
        _meta = record.get("metadata") or {}
        return (
            _meta.get("agent_name")   # SDK-supplied node name (preferred)
            or record.get("agent_id") # raw audit column
            or "unknown"
        )
    if group_by == "content_role":
        return record.get("role") or "unknown"
    if group_by == "use_case":
        meta = record.get("metadata") or {}
        return meta.get("use_case") or "unknown"
    if group_by == "function_name":
        return record.get("function_name") or "unknown"
    return "all"  # group_by == "none"


def _build_trend(records: list, dim_id: Optional[str], days: int) -> list:
    """
    Build a per-day trend series for the last `days` days.

    Each element: {"date": "YYYY-MM-DD", "eval_count": N, "violation_count": N}

    When dim_id is supplied: violation_count = evaluations where that dimension
    is WARNING or FLAGGED.  When dim_id is None: violation_count = any violation.
    """
    now = datetime.now(timezone.utc)
    buckets: dict[str, dict] = {}
    for d in range(days):
        day = (now - timedelta(days=d)).strftime("%Y-%m-%d")
        buckets[day] = {"date": day, "eval_count": 0, "violation_count": 0}

    for r in records:
        ts_str = r.get("timestamp", "")
        try:
            ts  = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            day = ts.strftime("%Y-%m-%d")
        except (ValueError, AttributeError):
            continue
        if day not in buckets:
            continue
        buckets[day]["eval_count"] += 1
        violated = (
            _dim_violation(r, dim_id) if dim_id
            else _is_violation(r)
        )
        if violated:
            buckets[day]["violation_count"] += 1

    # Return ascending order (oldest first — chart convention)
    return sorted(buckets.values(), key=lambda x: x["date"])


def _dim_aggregates(records: list) -> dict:
    """
    Compute per-dimension aggregates across a set of records.

    Returns: {dim_id: {avg_score, flagged_count, violation_rate_pct, sample}}
    """
    agg: dict[str, dict] = defaultdict(lambda: {"scores": [], "flagged": 0})

    for r in records:
        for dr in r.get("dimension_results", []):
            dim = dr.get("dimension")
            s   = dr.get("score")
            if not dim or s is None:
                continue
            agg[dim]["scores"].append(s)
            if dr.get("status") in ("WARNING", "FLAGGED"):
                agg[dim]["flagged"] += 1

    result = {}
    for dim, d in agg.items():
        sc  = d["scores"]
        n   = len(sc)
        avg = round(sum(sc) / n, 2) if n else None
        result[dim] = {
            "avg_score":          avg,
            "avg_normalized":     round((avg - 1) / 8, 3) if avg is not None else None,
            "flagged_count":      d["flagged"],
            "violation_rate_pct": round(d["flagged"] / n * 100, 1) if n else 0,
            "sample":             n,
        }
    return result


# ── Endpoint 1 — Per-app dimension tiles ─────────────────────────────────────

@router.get("/apps/{app_id}/dimensions")
def app_dimension_overview(
    app_id: str,
    days:   int     = Query(default=7,   ge=1,   le=90),
    db:     Session = Depends(get_db),
):
    """
    Return per-dimension metrics for one app with a 7-day trend.

    Used by the governance dashboard when an app tile is selected.  The
    response feeds the dimension tile grid (same shape as dim_scores in
    /v1/metrics/tier1, but scoped to this app).

    Response shape:
      {
        "app_id": "...",
        "period_days": 7,
        "dimensions": [
          {
            "dimension_id": "prompt_injection",
            "avg_score": 3.4,
            "flagged_count": 12,
            "violation_rate_pct": 18.5,
            "sample": 65,
            "trend": [
              {"date": "2026-04-30", "eval_count": 10, "violation_count": 2},
              ...
            ]
          },
          ...
        ]
      }
    """
    since   = datetime.now(timezone.utc) - timedelta(days=days)
    # "__all__" is a UI sentinel meaning "no app filter"
    _app_filter = None if app_id == "__all__" else app_id
    records = load_audit_records(db, limit=2000, since=since, app_id=_app_filter)

    if not records:
        return {"app_id": app_id, "period_days": days, "dimensions": []}

    agg   = _dim_aggregates(records)
    trend = _build_trend(records, dim_id=None, days=days)

    dimensions = []
    for dim_id, stats in sorted(
        agg.items(),
        key=lambda x: (-x[1]["flagged_count"], x[0]),
    ):
        dim_trend = _build_trend(records, dim_id=dim_id, days=days)
        dimensions.append({
            "dimension_id":       dim_id,
            "avg_score":          stats["avg_score"],
            "avg_normalized":     stats["avg_normalized"],
            "flagged_count":      stats["flagged_count"],
            "violation_rate_pct": stats["violation_rate_pct"],
            "sample":             stats["sample"],
            "trend":              dim_trend,
        })

    return {
        "app_id":      app_id,
        "period_days": days,
        "total_trend": trend,
        "dimensions":  dimensions,
    }


# ── Endpoint 2 — Evaluations for a specific dimension ─────────────────────────

@router.get("/apps/{app_id}/dimensions/{dim_id}/evaluations")
def dimension_evaluations(
    app_id:          str,
    dim_id:          str,
    days:            int     = Query(default=7,     ge=1,  le=90),
    violations_only: bool    = Query(default=True),
    group_by:        str     = Query(
        default="none",
        description="Group results by: agent_id | content_role | use_case | function_name | none",
    ),
    limit:           int     = Query(default=100,  ge=1,  le=500),
    verdict:         Optional[str] = Query(default=None, description="Filter by verdict: COERCIVE | NORMATIVE | PERMISSIVE"),
    db:              Session = Depends(get_db),
):
    """
    Evaluation list for a specific app + dimension.

    Supports:
    • violations_only=true — returns only evaluations where dim_id is WARNING/FLAGGED
    • group_by — summarises evaluations into groups (agent, role, use_case, function)
    • days     — rolling day window (default 7)

    Response shape:
      {
        "app_id": "...", "dimension_id": "...",
        "period_days": 7,
        "trend": [...],            # per-day counts for this dimension
        "groups": [                # present when group_by != "none"
          {
            "group_key": "transfer_funds",
            "eval_count": 14,
            "violation_count": 8,
            "avg_score": 6.2
          }, ...
        ],
        "evaluations": [           # individual records (after filters)
          {
            "interaction_id": "...",
            "timestamp": "...",
            "agent_id": "...",
            "role": "...",
            "verdict": "...",
            "dim_score": 7,
            "dim_status": "FLAGGED",
            "function_name": "transfer_funds",
            "content_snippet": "...",
            "arize_trace_url": null
          }, ...
        ]
      }
    """
    valid_groups = {"agent_id", "content_role", "use_case", "function_name", "none"}
    if group_by not in valid_groups:
        raise HTTPException(
            status_code=422,
            detail=f"group_by must be one of: {', '.join(sorted(valid_groups))}",
        )

    since   = datetime.now(timezone.utc) - timedelta(days=days)
    _app_filter = None if app_id == "__all__" else app_id
    _verdict_filter = verdict.upper() if verdict else None
    records = load_audit_records(db, limit=limit * 3, since=since, app_id=_app_filter, verdict=_verdict_filter)

    # Filter to records that include dim_id in their dimension_results
    dim_records = [
        r for r in records
        if any(dr.get("dimension") == dim_id for dr in r.get("dimension_results", []))
    ]

    if violations_only:
        dim_records = [r for r in dim_records if _dim_violation(r, dim_id)]

    # Trend (always based on ALL dim records, before violations_only filter)
    all_dim_records = [
        r for r in records
        if any(dr.get("dimension") == dim_id for dr in r.get("dimension_results", []))
    ]
    trend = _build_trend(all_dim_records, dim_id=dim_id, days=days)

    # Groups (on the post-filter set)
    groups_map: dict[str, dict] = defaultdict(
        lambda: {"eval_count": 0, "violation_count": 0, "scores": []}
    )
    for r in dim_records:
        key = _group_key(r, group_by)
        s   = _dim_score(r, dim_id)
        groups_map[key]["eval_count"] += 1
        if _dim_violation(r, dim_id):
            groups_map[key]["violation_count"] += 1
        if s is not None:
            groups_map[key]["scores"].append(s)

    groups_out = []
    if group_by != "none":
        for key, gd in sorted(
            groups_map.items(),
            key=lambda x: -x[1]["violation_count"],
        ):
            sc  = gd["scores"]
            avg = round(sum(sc) / len(sc), 2) if sc else None
            groups_out.append({
                "group_key":       key,
                "eval_count":      gd["eval_count"],
                "violation_count": gd["violation_count"],
                "avg_score":       avg,
            })

    # Individual evaluation list (capped at limit)
    evals_out = []
    for r in dim_records[:limit]:
        s      = _dim_score(r, dim_id)
        status = next(
            (dr.get("status") for dr in r.get("dimension_results", [])
             if dr.get("dimension") == dim_id),
            None,
        )
        _meta = r.get("metadata") or {}
        evals_out.append({
            "interaction_id":  r["interaction_id"],
            "timestamp":       r.get("timestamp"),
            # Prefer metadata.agent_name (specific node) over generic client agent_id
            "agent_id":        _meta.get("agent_name") or r.get("agent_id"),
            "role":            r.get("role"),
            "verdict":         r.get("verdict"),
            "session_id":      r.get("session_id"),
            "dim_score":       s,
            "dim_status":      status,
            # function_name extracted from metadata by load_audit_records; also check tool_name
            "function_name":   r.get("function_name") or _meta.get("tool_name"),
            "content_snippet": r.get("content_snippet"),
            "arize_trace_url": r.get("arize_trace_url"),
        })

    return {
        "app_id":       app_id,
        "dimension_id": dim_id,
        "period_days":  days,
        "total_matching": len(dim_records),
        "trend":        trend,
        "groups":       groups_out,
        "evaluations":  evals_out,
    }


# ── Endpoint 3 — Full evaluation detail (Evaluation Trail) ────────────────────

@router.get("/evaluations/{interaction_id}")
def evaluation_detail(
    interaction_id: str,
    db:             Session = Depends(get_db),
):
    """
    Full audit record for one interaction.

    Used by the Evaluation Trail panel when a user clicks a row in the
    evaluations list.  Returns all dimension scores, pack trace, content
    snippet (if stored), and arize_trace_url (if populated).

    Response shape:
      {
        "interaction_id": "...",
        "app_id": "...",
        "timestamp": "...",
        "role": "user_prompt",
        "verdict": "COERCIVE",
        "agent_id": "...",
        "session_id": "...",
        "function_name": "transfer_funds",
        "content_snippet": "Please ignore previous...",
        "content_hash": "a3f9...",
        "arize_trace_url": null,
        "dimension_results": [
          {"dimension": "prompt_injection", "score": 7, "status": "FLAGGED", ...},
          ...
        ],
        "violations": [...],
        "pack_trace": [...],
        "latency_ms": 42,
        "fail_reason": null
      }
    """
    # Load a small recent window — interaction_id is unique so we only need 1 row.
    # Using a 90-day window to cover reasonable audit retention.
    since   = datetime.now(timezone.utc) - timedelta(days=90)
    records = load_audit_records(db, limit=2000, since=since)

    match = next(
        (r for r in records if r.get("interaction_id") == interaction_id),
        None,
    )

    if match is None:
        # Retry without time filter for older records
        all_records = load_audit_records(db, limit=5000)
        match = next(
            (r for r in all_records if r.get("interaction_id") == interaction_id),
            None,
        )

    if match is None:
        raise HTTPException(status_code=404, detail=f"Interaction '{interaction_id}' not found")

    return {
        "interaction_id":    match["interaction_id"],
        "app_id":            match.get("app_id"),
        "timestamp":         match.get("timestamp"),
        "role":              match.get("role"),
        "verdict":           match.get("verdict"),
        "session_verdict":   match.get("session_verdict"),
        "session_escalated": match.get("session_escalated"),
        "agent_id":          match.get("agent_id"),
        "session_id":        match.get("session_id"),
        "function_name":     match.get("function_name"),
        "content_snippet":   match.get("content_snippet"),
        "content_hash":      match.get("content_hash"),
        "arize_trace_url":   match.get("arize_trace_url"),
        "dimension_results": match.get("dimension_results", []),
        "violations":        match.get("violations", []),
        "pack_trace":        match.get("pack_trace", []),
        "latency_ms":        match.get("latency_ms"),
        "model_id":          match.get("model_id"),
        "input_tokens":      match.get("input_tokens"),
        "output_tokens":     match.get("output_tokens"),
        "fail_reason":       match.get("fail_reason"),
        "metadata":          match.get("metadata"),
    }


# ── Endpoint 4 — LLM-powered violation explainer ─────────────────────────────

@router.get("/apps/{app_id}/dimensions/{dim_id}/explain")
async def dimension_explain(
    app_id:  str,
    dim_id:  str,
    days:    int     = Query(default=7, ge=1, le=90),
    db:      Session = Depends(get_db),
):
    """
    Three-bullet factual summary scoped to data already available in the DDM.

    Bullet 1 — Trend : shape of the daily violation curve over the window.
    Bullet 2 — Agents: which agents carry violations, at what rate (counts only).
    Bullet 3 — Roles : how violations distribute across content roles (counts only).

    No speculation, no causal inference, no recommendations — those belong to the
    Sentinel / Coordinator layer.  This endpoint only narrates what the numbers show.

    Provider pick order: Anthropic → Groq → Ollama (phi).
    Falls back to rule-based if no LLM provider is configured.

    Response shape:
      {
        "dimension_id": "sensitive_data_disclosure",
        "app_id":       "mybank",
        "period_days":  7,
        "mode":         "llm" | "rule_based",
        "provider":     "groq/llama-3.1-8b-instant" | null,
        "bullets":      ["...", "...", "..."],
        "raw_context":  { ... }
      }
    """
    since       = datetime.now(timezone.utc) - timedelta(days=days)
    _app_filter = None if app_id == "__all__" else app_id
    records     = load_audit_records(db, limit=3000, since=since, app_id=_app_filter)

    # Filter to records that involve this dimension
    dim_records  = [
        r for r in records
        if any(dr.get("dimension") == dim_id for dr in r.get("dimension_results", []))
    ]
    viol_records = [r for r in dim_records if _dim_violation(r, dim_id)]

    total      = len(dim_records)
    viol_count = len(viol_records)
    viol_pct   = round(viol_count / total * 100, 1) if total else 0

    # ── Daily trend (oldest → newest) ────────────────────────────────────────
    trend       = _build_trend(dim_records, dim_id=dim_id, days=days)
    trend_viols = [d["violation_count"] for d in trend]   # e.g. [0, 1, 2, 5, 3, 1, 4]

    # ── Actor + role aggregation ──────────────────────────────────────────────
    # __unattributed__ is tracked internally but excluded from prompt / bullets
    # (it is an SDK instrumentation gap, not a meaningful actor)
    actor_viol : dict[str, int] = defaultdict(int)
    actor_total: dict[str, int] = defaultdict(int)
    role_viol  : dict[str, int] = defaultdict(int)
    role_total : dict[str, int] = defaultdict(int)

    for r in dim_records:
        role  = r.get("role", "unknown")
        _meta = r.get("metadata") or {}
        ag    = _meta.get("agent_name") or r.get("agent_id")
        key   = "__user_input__" if role == "user_prompt" else (ag or "__unattributed__")
        actor_total[key] += 1
        role_total[role]  += 1

    for r in viol_records:
        role  = r.get("role", "unknown")
        _meta = r.get("metadata") or {}
        ag    = _meta.get("agent_name") or r.get("agent_id")
        key   = "__user_input__" if role == "user_prompt" else (ag or "__unattributed__")
        actor_viol[key] += 1
        role_viol[role]  += 1

    def _agent_lbl(k: str) -> str:
        return "User Input" if k == "__user_input__" else k

    # ── Additional context signals for the LLM ────────────────────────────────
    # Average violation score (1–9 scale; FLAGGED ≥ 7, WARNING 5–6)
    viol_scores  = [_dim_score(r, dim_id) for r in viol_records]
    viol_scores  = [s for s in viol_scores if s is not None]
    avg_score    = round(sum(viol_scores) / len(viol_scores), 1) if viol_scores else None

    def _score_bucket(s: Optional[float]) -> str:
        if s is None:   return "no score data"
        if s >= 7:      return f"{s}/9 — high severity (FLAGGED range)"
        if s >= 5:      return f"{s}/9 — moderate severity (WARNING range)"
        return f"{s}/9 — low severity (PASS range)"

    # Unique sessions that contributed violations
    session_count = len({r.get("session_id") for r in dim_records if r.get("session_id")})

    # Ingress-only: all violations caught at user_prompt before any agent acted
    # (typical for prompt injection / jailbreak dimensions)
    ingress_only  = bool(actor_viol) and all(
        k in ("__user_input__", "__unattributed__") for k in actor_viol
    )

    # Named agents (excluding user_input and unattributed)
    active_agents = [
        k for k in actor_viol
        if k not in ("__user_input__", "__unattributed__")
    ]

    # Top 5 attributed actors, unattributed excluded
    agent_lines = [
        f"{_agent_lbl(k)}: {actor_viol[k]}v / {actor_total.get(k, actor_viol[k])} evals"
        for k in sorted(actor_viol, key=lambda x: -actor_viol[x])
        if k != "__unattributed__"
    ][:5]

    role_lines = [
        f"{k.replace('_', ' ')}: {role_viol[k]}v / {role_total.get(k, role_viol[k])} evals"
        for k in sorted(role_viol, key=lambda x: -role_viol[x])
    ]

    raw_ctx = {
        "total_evals":    total,
        "violations":     viol_count,
        "violation_pct":  viol_pct,
        "avg_score":      avg_score,
        "session_count":  session_count,
        "ingress_only":   ingress_only,
        "active_agents":  len(active_agents),
        "trend_daily":    trend_viols,
        "by_actor":       {k: v for k, v in actor_viol.items() if k != "__unattributed__"},
        "by_role":        dict(role_viol),
    }

    # ── Try LLM (provider pick: Anthropic → Groq → Ollama) ───────────────────
    # Anthropic: tool use enforces {"trend","agents","roles"} at protocol level.
    # Groq:      JSON-object mode enforces JSON; system prompt specifies keys.
    # Ollama:    free-text; line parser used as fallback within this block.
    llm_fn, llm_label = _get_llm()
    if llm_fn and total > 0:
        agent_str = "\n".join(f"  - {l}" for l in agent_lines) or "  - (no agents)"
        role_str  = "\n".join(f"  - {l}" for l in role_lines)  or "  - (no roles)"

        system_prompt = (
            "You are a data narrator for an enterprise AI governance platform. "
            "Produce exactly 3 factual observations about guardrail violations. "
            'Return JSON with keys "trend", "agents", "roles". One sentence per key, under 25 words.\n'
            "Rules — follow strictly:\n"
            "1. Use absolute counts, not percentage changes, when total violations < 20 (sparse data).\n"
            "2. Do NOT repeat the same information across fields. "
            "If agents and roles describe the same population, use the roles field "
            "to add information about severity or session spread instead.\n"
            "3. If ingress_only=true, the agents field must state that all violations "
            "were intercepted at the user-input stage before any agent acted — "
            "frame this as a guardrail effectiveness signal, not a risk.\n"
            "4. Scores are 1–9: FLAGGED ≥ 7 (high risk), WARNING 5–6 (moderate). "
            "Reference the severity band, not the raw number alone.\n"
            "5. No causal claims, no recommendations."
        )
        sparse      = viol_count < 20
        user_prompt = (
            f"Dimension: {dim_id.replace('_', ' ').title()} | App: {app_id} | "
            f"Window: last {days} days | Sessions: {session_count}\n"
            f"Total evals: {total} | Violations: {viol_count} ({viol_pct}%) | "
            f"Avg violation score: {_score_bucket(avg_score)}\n"
            f"Data volume: {'sparse' if sparse else 'sufficient'} "
            f"({'use absolute counts' if sparse else 'percentages are meaningful'})\n"
            f"Ingress-only (all violations at user-prompt stage): {ingress_only}\n"
            f"Named agents with violations: {len(active_agents)} "
            f"({', '.join(active_agents[:3]) or 'none'})\n"
            f"Daily violations (oldest→newest): {trend_viols}\n\n"
            f"By agent:\n{agent_str}\n\n"
            f"By content role:\n{role_str}"
        )
        try:
            raw_text = await llm_fn(system_prompt, user_prompt)

            # ── Structured parse (Anthropic tool use / Groq JSON mode) ──────────
            # Try JSON first — this always succeeds for Anthropic and Groq.
            bullets: list[str] = []
            try:
                data    = json.loads(raw_text)
                bullets = [
                    data.get("trend",  "").strip(),
                    data.get("agents", "").strip(),
                    data.get("roles",  "").strip(),
                ]
                bullets = [b for b in bullets if len(b) > 5]
            except (json.JSONDecodeError, AttributeError, TypeError):
                # Ollama or unexpected free-text response — extract bullet lines
                logger.debug("[explain] JSON parse failed for %s — using line parser", llm_label)
                bullets = [
                    line.lstrip("-•·– ").strip()
                    for line in raw_text.splitlines()
                    if line.strip() and line.strip()[0] in "-•·–"
                ]
                if not bullets:
                    bullets = [l.strip() for l in raw_text.splitlines() if l.strip()]
                bullets = [b for b in bullets if len(b) > 5][:3]

            if bullets:
                return {
                    "dimension_id": dim_id,
                    "app_id":       app_id,
                    "period_days":  days,
                    "mode":         "llm",
                    "provider":     llm_label,
                    "bullets":      bullets,
                    "raw_context":  raw_ctx,
                }
        except Exception as exc:
            logger.warning("[explain] LLM call failed (%s), falling back to rules: %s", llm_label, exc)

    # ── Rule-based fallback — same 3-bullet structure ─────────────────────────
    bullets: list[str] = []

    if total == 0:
        bullets.append("No evaluations recorded for this dimension in the selected window.")
        return {
            "dimension_id": dim_id,
            "app_id":       app_id,
            "period_days":  days,
            "mode":         "rule_based",
            "provider":     None,
            "bullets":      bullets,
            "raw_context":  raw_ctx,
        }

    # Bullet 1 — trend direction
    half        = max(len(trend_viols) // 2, 1)
    first_half  = sum(trend_viols[:half])
    second_half = sum(trend_viols[half:])
    peak_val    = max(trend_viols) if trend_viols else 0
    peak_day    = trend[trend_viols.index(peak_val)]["date"] if peak_val and trend else None

    if second_half > first_half * 1.5 and second_half > 0:
        bullets.append(
            f"Violations rising — {second_half} in the second half of the window "
            f"vs {first_half} in the first half."
        )
    elif first_half > second_half * 1.5 and first_half > 0:
        bullets.append(
            f"Violations declining — {first_half} in the first half of the window "
            f"vs {second_half} in the second half."
        )
    elif peak_val >= 3 and trend_viols.count(0) >= len(trend_viols) // 2:
        bullets.append(
            f"Spike pattern — {peak_val} violations on {peak_day}, "
            f"otherwise low across the window."
        )
    else:
        bullets.append(
            f"{viol_count} violations across the {days}-day window, "
            f"averaging {round(viol_count / days, 1)} per day."
        )

    # Bullet 2 — agent summary (top 2 attributed actors)
    top_agents = sorted(
        [(k, v) for k, v in actor_viol.items() if k != "__unattributed__"],
        key=lambda x: -x[1],
    )[:2]
    if top_agents:
        parts = []
        for ag, cnt in top_agents:
            ag_tot = actor_total.get(ag, cnt)
            ag_pct = round(cnt / ag_tot * 100) if ag_tot else 0
            parts.append(f"{_agent_lbl(ag)} {cnt}/{ag_tot} ({ag_pct}%)")
        bullets.append(f"Agents: {' · '.join(parts)}.")
    else:
        bullets.append("No agent attribution data available for this window.")

    # Bullet 3 — role distribution
    if role_viol:
        top_roles  = sorted(role_viol.items(), key=lambda x: -x[1])[:3]
        role_parts = [
            f"{r.replace('_', ' ')} {v}/{role_total.get(r, v)}"
            for r, v in top_roles
        ]
        bullets.append(f"Roles: {' · '.join(role_parts)}.")
    else:
        bullets.append("No content role data available for this window.")

    return {
        "dimension_id": dim_id,
        "app_id":       app_id,
        "period_days":  days,
        "mode":         "rule_based",
        "provider":     None,
        "bullets":      bullets,
        "raw_context":  raw_ctx,
    }
