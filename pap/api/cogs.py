"""
LLM Token Costs API.

Computes LLM token costs for governed applications by joining:
  - Token data from audit_evaluation_audit (model_id / input_tokens / output_tokens
    columns written by the SqliteAuditWriter in the Policy Engine)
  - Pricing reference from config_model_pricing

Data source: audit_evaluation_audit SQLite table.
The PE writes every evaluation to this table via SqliteAuditWriter; the
model_id / input_tokens / output_tokens columns are populated from the
agent_tokens metadata field written by the RAI Trust SDK's on_llm_end callback.

Deduplication: the SDK sends cumulative token counts per node on every evaluation
within a session turn (tool_call, agent_to_agent, etc.).  We take the maximum
snapshot per (session_id-or-interaction_id, model_id) to avoid double-counting
within a single session.  This is handled entirely in SQL via a subquery.

Endpoints:
  GET /v1/cogs                 — cost breakdown for one app over a time window
  GET /v1/cogs/summary         — platform-wide cost summary across all apps
  GET /v1/cogs/pricing         — list seeded model pricing reference data
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.database import get_db
from core.models import ModelPricing

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/cogs", tags=["LLM Token Costs"])

# ── Window helpers ─────────────────────────────────────────────────────────────

_WINDOW_DELTA: dict[str, timedelta] = {
    "1h":  timedelta(hours=1),
    "6h":  timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d":  timedelta(days=7),
    "30d": timedelta(days=30),
}


def _window_start(window: str) -> datetime:
    delta = _WINDOW_DELTA.get(window, timedelta(hours=24))
    return datetime.now(timezone.utc) - delta


# ── Pricing lookup ─────────────────────────────────────────────────────────────

def _get_pricing_map(db: Session) -> dict[str, tuple[float, float]]:
    """Returns {model_id: (cost_per_1m_input, cost_per_1m_output)} for all active rows."""
    rows = db.query(ModelPricing).filter_by(active=True, effective_until=None).all()
    return {r.model_id.lower(): (r.cost_per_1m_input, r.cost_per_1m_output) for r in rows}


def _match_price(model_name: str | None, pricing_map: dict) -> tuple[float, float] | None:
    """
    Fuzzy-match a model name from audit_evaluation_audit against the pricing table.
    Tries exact match first, then substring containment both ways.
    """
    if not model_name:
        return None
    key = model_name.lower()
    if key in pricing_map:
        return pricing_map[key]
    for pk, pv in pricing_map.items():
        if pk in key or key in pk:
            return pv
    return None


# ── Token aggregation from SQLite ──────────────────────────────────────────────

def _token_rows(app_id: Optional[str], since: datetime, db: Session) -> list[dict]:
    """
    Query audit_evaluation_audit for per-(app_id, model_id) token aggregates.

    Deduplication (mirrors the JSONL approach):
      The SDK writes cumulative token snapshots per LLM node on each evaluation
      event within a session turn.  We keep MAX(input_tokens) and MAX(output_tokens)
      per (COALESCE(session_id, interaction_id), model_id) group, then SUM those
      maxima by (app_id, model_id) to get the true per-app total.
    """
    since_str = since.replace(tzinfo=None).isoformat(timespec="seconds")

    app_filter = "AND a.app_id = :app_id" if app_id else ""
    params: dict = {"since": since_str}
    if app_id:
        params["app_id"] = app_id

    sql = text(f"""
        SELECT
            app_id,
            model_id,
            SUM(max_input)  AS input_tokens,
            SUM(max_output) AS output_tokens,
            COUNT(*)        AS eval_count
        FROM (
            SELECT
                a.app_id,
                a.model_id,
                COALESCE(a.session_id, a.interaction_id) AS grp,
                MAX(COALESCE(a.input_tokens,  0))        AS max_input,
                MAX(COALESCE(a.output_tokens, 0))        AS max_output
            FROM audit_evaluation_audit a
            WHERE a.timestamp >= :since
              AND a.model_id   IS NOT NULL
              AND (a.input_tokens > 0 OR a.output_tokens > 0)
              {app_filter}
            GROUP BY a.app_id, a.model_id,
                     COALESCE(a.session_id, a.interaction_id)
        ) sub
        GROUP BY app_id, model_id
        ORDER BY input_tokens DESC
    """)

    rows = db.execute(sql, params).fetchall()
    return [
        {
            "app_id":        r.app_id   or "unknown",
            "model_name":    r.model_id or "unknown",
            "input_tokens":  int(r.input_tokens  or 0),
            "output_tokens": int(r.output_tokens or 0),
            "eval_count":    int(r.eval_count),
        }
        for r in rows
    ]


# ── Cost computation ───────────────────────────────────────────────────────────

def _compute_cost(rows: list[dict], pricing_map: dict) -> list[dict]:
    """Enrich token rows with cost computation. Unmatched models get cost_usd=None."""
    enriched = []
    for r in rows:
        price = _match_price(r["model_name"], pricing_map)
        cost_usd: float | None = None
        if price and (r["input_tokens"] or r["output_tokens"]):
            cin, cout = price
            cost_usd = round(
                (r["input_tokens"] * cin  / 1_000_000)
                + (r["output_tokens"] * cout / 1_000_000),
                6,
            )
        enriched.append({
            "app_id":        r["app_id"],
            "model":         r["model_name"],
            "input_tokens":  r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "eval_count":    r["eval_count"],
            "cost_usd":      cost_usd,
            "price_matched": price is not None,
        })
    return enriched


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("")
def get_cogs(
    app_id: Optional[str] = Query(None, description="Filter to a specific app"),
    window: str            = Query("24h", description="Time window: 1h | 6h | 24h | 7d | 30d"),
    db: Session            = Depends(get_db),
):
    """
    LLM token cost breakdown for one app (or all apps) over the given time window.

    Returns per-model token counts, computed cost in USD, and a totals summary.
    Token data is read from audit_evaluation_audit (written by SqliteAuditWriter).
    Models not found in config_model_pricing are reported with price_matched=false.
    """
    since       = _window_start(window)
    pricing_map = _get_pricing_map(db)
    rows        = _token_rows(app_id, since, db)
    enriched    = _compute_cost(rows, pricing_map)

    total_input  = sum(r["input_tokens"]  for r in enriched)
    total_output = sum(r["output_tokens"] for r in enriched)
    total_evals  = sum(r["eval_count"]    for r in enriched)
    total_cost   = sum(r["cost_usd"] or 0 for r in enriched)
    avg_cost     = round(total_cost / total_evals, 8) if total_evals else 0.0

    return {
        "window":  window,
        "since":   since.isoformat(),
        "app_id":  app_id or "all",
        "summary": {
            "total_input_tokens":  total_input,
            "total_output_tokens": total_output,
            "total_tokens":        total_input + total_output,
            "total_interactions":  total_evals,
            "total_cost_usd":      round(total_cost, 6),
            "avg_cost_per_interaction_usd": avg_cost,
        },
        "by_model": enriched,
    }


@router.get("/summary")
def get_cogs_summary(
    window: str = Query("24h", description="Time window: 1h | 6h | 24h | 7d | 30d"),
    db: Session = Depends(get_db),
):
    """
    Platform-wide LLM token cost summary across all apps.
    Groups by app first, then model within each app.
    """
    since       = _window_start(window)
    pricing_map = _get_pricing_map(db)
    rows        = _token_rows(None, since, db)
    enriched    = _compute_cost(rows, pricing_map)

    by_app: dict[str, dict] = {}
    for r in enriched:
        app = r["app_id"] or "unknown"
        if app not in by_app:
            by_app[app] = {
                "app_id":             app,
                "total_cost_usd":     0.0,
                "total_tokens":       0,
                "total_interactions": 0,
                "models":             [],
            }
        by_app[app]["total_cost_usd"]     += r["cost_usd"] or 0
        by_app[app]["total_tokens"]       += r["input_tokens"] + r["output_tokens"]
        by_app[app]["total_interactions"] += r["eval_count"]
        by_app[app]["models"].append(r)

    apps_list   = sorted(by_app.values(), key=lambda a: a["total_cost_usd"], reverse=True)
    grand_total = sum(a["total_cost_usd"] for a in apps_list)

    return {
        "window":               window,
        "since":                since.isoformat(),
        "grand_total_cost_usd": round(grand_total, 6),
        "app_count":            len(apps_list),
        "apps":                 apps_list,
    }


@router.get("/pricing")
def get_pricing(
    provider: Optional[str] = Query(None, description="Filter by provider"),
    db: Session             = Depends(get_db),
):
    """List model pricing reference data from config_model_pricing."""
    q = db.query(ModelPricing).filter_by(active=True, effective_until=None)
    if provider:
        q = q.filter_by(provider=provider.lower())
    rows = q.order_by(ModelPricing.provider, ModelPricing.model_id).all()
    return {
        "count":  len(rows),
        "models": [r.to_dict() for r in rows],
    }
