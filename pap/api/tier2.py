"""
Tier 2 Evaluation Scores API.

Tier 2 is the observability-backend layer — in production this reads from
Arize Phoenix via the TrustDataProvider SPI.  Until Arize is connected,
this module provides a full simulation by projecting audit_evaluation_dimensions
records into the tier2_eval_scores table using the same schema that the
Phoenix annotation API exposes.

How the simulation works
------------------------
  * On every GET /v1/metrics/tier2 request, _sync_from_audit() is called first.
  * It reads all (interaction_id, dimension_id) pairs in audit_evaluation_dimensions
    that are NOT yet in tier2_eval_scores (idempotent — duplicate-safe).
  * Each dimension score (1–9 raw → 0.0–1.0 normalised) becomes one T2 row:
      eval_name      = dimension_id
      score          = score_normalized
      label          = PASS | NEEDS_REVIEW | FAIL  (derived from score_normalized)
      annotator_kind = COMPUTED
      provider_id    = computed_from_audit
  * The GET endpoint then aggregates the tier2_eval_scores rows and returns
    per-dimension pass rates and average scores.

When Arize is integrated
------------------------
  * Replace _sync_from_audit() with a PhoenixProvider that reads the live
    Arize GraphQL Metrics API.
  * Keep the /v1/metrics/tier2 response shape identical — the UI doesn't change.

Endpoints
---------
  GET /v1/metrics/tier2           — aggregated scores per dimension (with optional filters)
  GET /v1/metrics/tier2/raw       — raw tier2_eval_scores rows (newest-first)
  POST /v1/metrics/tier2/sync     — trigger manual backfill from audit dimensions
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.database import get_db
from core.models import Tier2EvalScore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/metrics/tier2", tags=["Tier 2 Scores"])


# ── Label thresholds (mirrors Arize annotation convention) ───────────────────

def _score_to_label(score_normalized: float | None) -> str:
    if score_normalized is None:
        return "NEEDS_REVIEW"
    if score_normalized >= 0.70:
        return "PASS"
    if score_normalized >= 0.40:
        return "NEEDS_REVIEW"
    return "FAIL"


# ── Sync: project audit_evaluation_dimensions → tier2_eval_scores ─────────────

def sync_tier2_scores(db: Session) -> int:
    """
    Idempotent backfill from audit_evaluation_dimensions into tier2_eval_scores.

    Inserts a T2 row for every (interaction_id, dimension_id) pair in the audit
    tables that is not yet represented in tier2_eval_scores.

    Returns the count of newly inserted rows.
    """
    # Find audit dimension rows that have no matching T2 row yet.
    # Join path: audit_evaluation_dimensions → audit_evaluation_audit
    #            to get interaction_id and app_id.
    # LEFT JOIN + WHERE t2.id IS NULL = "not yet synced" pattern.
    new_rows_sql = text("""
        SELECT
            a.interaction_id,
            a.app_id,
            a.enterprise_id,
            a.agent_id,
            a.timestamp,
            d.dimension_id,
            d.score_normalized,
            d.score
        FROM audit_evaluation_dimensions d
        JOIN audit_evaluation_audit a ON a.id = d.evaluation_audit_id
        LEFT JOIN tier2_eval_scores t2
               ON t2.interaction_id = a.interaction_id
              AND t2.eval_name      = d.dimension_id
        WHERE t2.id IS NULL
        ORDER BY a.timestamp DESC
        LIMIT 5000
    """)

    try:
        rows = db.execute(new_rows_sql).fetchall()
    except Exception:
        logger.exception("[tier2] Error reading audit data for sync")
        return 0

    if not rows:
        return 0

    inserted = 0
    for r in rows:
        # audit_evaluation_dimensions.score_normalized = (score - 1) / 8.0
        # This is a "badness" score: 0.0 = clean/pass, 1.0 = flagged/fail.
        #
        # Tier 2 follows the Arize/Phoenix annotation convention where
        # score=1.0 means PASS (high quality) and score=0.0 means FAIL.
        # Invert: tier2_score = 1.0 - score_normalized  (goodness score)
        if r.score_normalized is not None:
            tier2_score = round(1.0 - r.score_normalized, 4)
        elif r.score is not None:
            # score_normalized missing — compute goodness directly from raw 1–9 score
            tier2_score = round(1.0 - (r.score - 1) / 8.0, 4)
        else:
            tier2_score = None

        ts = r.timestamp
        if not isinstance(ts, datetime):
            ts = datetime.now(timezone.utc)

        db.add(Tier2EvalScore(
            interaction_id=r.interaction_id,
            app_id=r.app_id or "unknown",
            enterprise_id=r.enterprise_id,
            agent_id=r.agent_id,
            eval_name=r.dimension_id,
            score=tier2_score,
            label=_score_to_label(tier2_score),
            explanation=None,
            annotator_kind="COMPUTED",
            provider_id="computed_from_audit",
            evaluated_at=ts,
        ))
        inserted += 1

    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("[tier2] Error committing T2 sync rows")
        return 0

    logger.info(f"[tier2] Synced {inserted} new rows from audit dimensions")
    return inserted


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


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("")
def get_tier2_metrics(
    app_id: Optional[str] = Query(None, description="Filter to a specific app"),
    window: str            = Query("24h", description="Time window: 1h | 6h | 24h | 7d | 30d"),
    db: Session            = Depends(get_db),
):
    """
    Aggregated Tier 2 evaluation scores per dimension.

    Calls _sync_from_audit() on every request to pull in any new audit
    dimension rows before computing the aggregates — no separate cron needed.

    Response shape:
      {
        "window": "24h",
        "source": "computed_from_audit",
        "synced_rows": 12,
        "dimensions": [
          {
            "eval_name": "hate",
            "avg_score": 0.95,
            "pass_count": 48,
            "needs_review_count": 2,
            "fail_count": 0,
            "total": 50,
            "pass_rate_pct": 96.0
          },
          ...
        ]
      }
    """
    # Lazy sync on every read — keeps T2 table up to date without a scheduler
    synced = sync_tier2_scores(db)

    since     = _window_start(window)
    # SQLAlchemy stores datetime in SQLite with a space separator ("2026-04-27 15:34:05"),
    # not the ISO 8601 "T" separator.  Use space here so the WHERE comparison matches.
    since_str = since.replace(tzinfo=None).isoformat(timespec="seconds").replace("T", " ")

    params: dict = {"since": since_str}
    app_filter = ""
    if app_id:
        app_filter = "AND app_id = :app_id"
        params["app_id"] = app_id

    agg_sql = text(f"""
        SELECT
            eval_name,
            COUNT(*)                                                 AS total,
            ROUND(AVG(COALESCE(score, 0.0)), 4)                     AS avg_score,
            SUM(CASE WHEN label = 'PASS'         THEN 1 ELSE 0 END) AS pass_count,
            SUM(CASE WHEN label = 'NEEDS_REVIEW' THEN 1 ELSE 0 END) AS needs_review_count,
            SUM(CASE WHEN label = 'FAIL'         THEN 1 ELSE 0 END) AS fail_count
        FROM tier2_eval_scores
        WHERE evaluated_at >= :since
          {app_filter}
        GROUP BY eval_name
        ORDER BY fail_count DESC, eval_name
    """)

    rows = db.execute(agg_sql, params).fetchall()
    dimensions = []
    for r in rows:
        total = r.total or 0
        pass_rate = round(r.pass_count / total * 100, 1) if total else 0.0
        dimensions.append({
            "eval_name":          r.eval_name,
            "avg_score":          r.avg_score,
            "pass_count":         r.pass_count,
            "needs_review_count": r.needs_review_count,
            "fail_count":         r.fail_count,
            "total":              total,
            "pass_rate_pct":      pass_rate,
        })

    return {
        "window":      window,
        "since":       since.isoformat(),
        "app_id":      app_id or "all",
        "source":      "computed_from_audit",
        "synced_rows": synced,
        "dimensions":  dimensions,
    }


@router.get("/raw")
def get_tier2_raw(
    app_id:   Optional[str] = Query(None),
    eval_name: Optional[str] = Query(None, description="Filter to a specific dimension"),
    window:   str            = Query("24h"),
    limit:    int            = Query(200, ge=1, le=2000),
    db: Session              = Depends(get_db),
):
    """
    Raw tier2_eval_scores rows — newest-first.
    Useful for debugging and per-interaction drill-down.
    """
    since     = _window_start(window)
    # SQLAlchemy stores datetime in SQLite with a space separator ("2026-04-27 15:34:05"),
    # not the ISO 8601 "T" separator.  Use space here so the WHERE comparison matches.
    since_str = since.replace(tzinfo=None).isoformat(timespec="seconds").replace("T", " ")

    filters = ["evaluated_at >= :since"]
    params: dict = {"since": since_str, "limit": limit}
    if app_id:
        filters.append("app_id = :app_id")
        params["app_id"] = app_id
    if eval_name:
        filters.append("eval_name = :eval_name")
        params["eval_name"] = eval_name

    where = " AND ".join(filters)
    sql = text(f"""
        SELECT interaction_id, app_id, eval_name, score, label,
               annotator_kind, provider_id, evaluated_at
        FROM tier2_eval_scores
        WHERE {where}
        ORDER BY evaluated_at DESC
        LIMIT :limit
    """)

    rows = db.execute(sql, params).fetchall()
    return {
        "total": len(rows),
        "window": window,
        "rows": [
            {
                "interaction_id": r.interaction_id,
                "app_id":         r.app_id,
                "eval_name":      r.eval_name,
                "score":          r.score,
                "label":          r.label,
                "annotator_kind": r.annotator_kind,
                "provider_id":    r.provider_id,
                "evaluated_at":   r.evaluated_at,
            }
            for r in rows
        ],
    }


@router.post("/sync", status_code=200)
def manual_sync(
    reset: bool = Query(False, description="If true, truncate tier2_eval_scores before re-syncing"),
    db: Session = Depends(get_db),
):
    """
    Manually trigger a backfill of tier2_eval_scores from audit_evaluation_dimensions.
    Useful after importing historical data or after the first PE run.

    reset=true  — truncates the table first, then re-syncs all audit dimensions.
                  Use this after a score-direction fix or scoring logic change.
    """
    if reset:
        try:
            db.execute(text("DELETE FROM tier2_eval_scores"))
            db.commit()
            logger.info("[tier2] tier2_eval_scores truncated for reset sync")
        except Exception:
            db.rollback()
            logger.exception("[tier2] Error truncating tier2_eval_scores")

    inserted = sync_tier2_scores(db)
    total    = db.query(Tier2EvalScore).count()
    return {
        "message":      f"{'Reset + s' if reset else 'S'}ync complete — {inserted} new rows inserted",
        "rows_inserted": inserted,
        "total_t2_rows": total,
        "reset":         reset,
    }
