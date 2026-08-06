"""
App Trust Score API.

Computes a 0-100 composite trust score for governed applications by
aggregating category-level pass rates from audit evaluation data.

Formula:
  Step 1 — Category averages (per-category pass rate):
    For each record, find the worst (max) dimension score in that category.
    Normalise: normalised = 1.0 - (worst_score - 1) / 8   → [1.0 best … 0.0 worst]
    category_score = mean(normalised) across all records that contain that category's dimensions.
    Categories with NO evaluations in the window are EXCLUDED from the composite
    (their weight is redistributed to the observed categories via denominator normalisation).

  Step 2 — Weighted composite:
    composite = sum(weight_k × category_score_k) / sum(weight_k for observed categories only)
    Missing-category weights are effectively redistributed rather than set to 1.0 or 0.0.

  Step 3 — COERCIVE penalty (volume-independent deduction):
    penalty = min(COERCIVE_PENALTY_CAP, coercive_count × COERCIVE_PENALTY_PER_EVENT)
    adjusted_composite = max(0.0, composite - penalty)
    trust_score = round(adjusted_composite × 100)   → 0-100 integer

    Rationale: a simple average is easily diluted by high clean-traffic volume.
    The penalty term ensures that even rare COERCIVE events visibly lower the score
    regardless of how many permissive evaluations surround them.

Penalty constants (module-level, adjust for deployment context):
  COERCIVE_PENALTY_PER_EVENT = 0.03   (-3 pp per COERCIVE verdict)
  COERCIVE_PENALTY_CAP       = 0.35   (maximum -35 pp from this term)

Weights come from config_app_trust_weights (per-app or enterprise default row).
Hard-coded defaults: Safety 30%, Security 25%, Compliance 25%, Operational 10%, Quality 10%.

Category → dimension mapping follows the RAI pillar taxonomy (migration 0012/0013):
  safety      — hate, harassment, violence, sexual_content, self_harm, misconduct
  security    — prompt_injection, jailbreak, malicious_intent, sensitive_data_request,
                sensitive_data_disclosure
  compliance  — corporate_policy, aml_structuring_signal, sanctions_country_reference,
                sanctions_entity_mention, sanctions_evasion_signal,
                eu_ai_act_prohibited_practice, eu_ai_act_bias_discrimination,
                eu_ai_act_transparency, data_minimisation_violation
  operational — off_topic_request, prompt_repetition, session_risk_scorer
  quality     — groundedness (deferred to Tier 2 / Arize; category sparse until ingested)

Data source: audit_evaluation_audit + audit_evaluation_dimensions (SQLite).
Each compute optionally writes a row to metric_snapshots for time-series tracking.

Endpoints:
  GET /v1/trust-score          — score for one or all apps over a time window
  GET /v1/trust-score/history  — recent metric_snapshots rows (sparkline / trend data)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.database import get_db
from core.models import AppTrustWeights, MetricSnapshot

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/trust-score", tags=["Trust Score"])

# ── Category -> dimension mapping ──────────────────────────────────────────────
#
# These 5 trust-score buckets map to the AppTrustWeights DB columns and are
# stable across platform versions.  The DB's RAI pillar taxonomy (migration 0012)
# uses a finer-grained category set; dimensions are re-bucketed here into the
# 5-score structure so the composite formula remains consistent over time.
#
# Rules:
#   security    — adversarial/injection attacks + data exposure dimensions
#   compliance  — all regulatory obligations (financial crime, EU AI Act,
#                  corporate policy governance)
#   operational — prompt-quality signals and session-level health indicators
#   quality     — model output fidelity (groundedness deferred to Tier 2/Arize)
#
# If a new dimension is added to the DB catalog, add it here too.  Dimensions
# absent from _CATEGORY_DIMS are evaluated by providers but silently excluded
# from the trust score composite — an intentional escape hatch for monitoring-only dims.

_CATEGORY_DIMS: dict[str, set[str]] = {
    "safety": {
        "hate", "harassment", "violence", "sexual_content", "self_harm", "misconduct",
    },
    "security": {
        # Adversarial attacks — activated migration 0013
        "prompt_injection", "jailbreak", "malicious_intent",
        # Data exposure — regulated identifier disclosure (OWASP LLM02)
        "sensitive_data_request", "sensitive_data_disclosure",
    },
    "compliance": {
        # Corporate governance
        "corporate_policy",
        # Financial crime / AML / sanctions
        "aml_structuring_signal", "sanctions_country_reference",
        "sanctions_entity_mention", "sanctions_evasion_signal",
        # EU AI Act obligations — activated migration 0013
        "eu_ai_act_prohibited_practice",   # Art. 5  — prohibited practices
        "eu_ai_act_bias_discrimination",    # Art. 10 — non-discrimination
        "eu_ai_act_transparency",           # Art. 13 — AI identity disclosure
        # GDPR data minimisation — NORMATIVE; activated migration 0014
        "data_minimisation_violation",      # Art. 5(1)(c) — excess PII in output
    },
    "operational": {
        # Prompt-filter dimensions
        "off_topic_request", "prompt_repetition",
        # Session-level risk signals
        "session_risk_scorer",
    },
    # groundedness deferred to Tier 2 / Arize (migration 0013 decision).
    # Category intentionally sparse — no-data rebalance redistributes this
    # weight to observed categories until Arize ingestion is wired up.
    # NOTE: must be an empty *set* (set()), not an empty dict ({}), so the
    # `_CATEGORY_DIMS[cat] & seen_dims` intersection at compute time is valid.
    "quality": set(),
}

_DEFAULT_WEIGHTS: dict[str, float] = {
    # Safety and security remain the primary pillars.
    # Compliance raised to 0.25 (from 0.20) — EU AI Act + financial crime dims now active.
    # Quality lowered to 0.10 (from 0.15) — groundedness deferred; redistributes via
    # no-data rebalance to observed categories, so effective safety/security/compliance
    # weights are proportionally higher when quality has no data.
    "safety":      0.30,
    "security":    0.25,
    "compliance":  0.25,
    "operational": 0.10,
    "quality":     0.10,
}

# ── COERCIVE penalty constants ─────────────────────────────────────────────────
# Each COERCIVE verdict event deducts this many points from the composite (0-1 scale).
# The deduction is independent of traffic volume so it cannot be diluted by clean evals.
# Adjust these for deployment sensitivity; recommended range: 0.02–0.05 per event.
_COERCIVE_PENALTY_PER_EVENT: float = 0.03   # -3 percentage points per COERCIVE event
_COERCIVE_PENALTY_CAP:       float = 0.35   # maximum -35 pp from COERCIVE penalty alone

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


# ── SQLite reader ──────────────────────────────────────────────────────────────

def _read_records(
    since: datetime,
    app_id: Optional[str],
    db: Session,
    limit: int = 5000,
) -> list[dict]:
    """
    Read audit records from SQLite joined with their dimension scores.

    Returns a list of dicts with a 'dimension_results' key, compatible with
    _compute_trust_score().  Each dict mirrors the JSONL record structure so
    the computation logic is unchanged.
    """
    since_str  = since.replace(tzinfo=None).isoformat(timespec="seconds")
    app_filter = "AND a.app_id = :app_id" if app_id else ""
    params: dict = {"since": since_str, "limit": limit}
    if app_id:
        params["app_id"] = app_id

    # Fetch audit rows (newest-first, capped at limit)
    audit_sql = text(f"""
        SELECT a.id, a.interaction_id, a.app_id, a.session_id,
               a.verdict, a.timestamp
        FROM audit_evaluation_audit a
        WHERE a.timestamp >= :since
          {app_filter}
        ORDER BY a.timestamp DESC
        LIMIT :limit
    """)

    # Fetch all dimension rows for those audits in one query
    dim_sql = text(f"""
        SELECT d.evaluation_audit_id, d.dimension_id, d.score, d.status, d.provider
        FROM audit_evaluation_dimensions d
        JOIN audit_evaluation_audit a ON a.id = d.evaluation_audit_id
        WHERE a.timestamp >= :since
          {app_filter}
    """)

    audit_rows = db.execute(audit_sql, params).fetchall()
    dim_rows   = db.execute(dim_sql,   {k: v for k, v in params.items() if k != "limit"}).fetchall()

    # Build dimension lookup: audit_id -> [dimension_result, ...]
    dims_by_id: dict[int, list[dict]] = {}
    for d in dim_rows:
        dims_by_id.setdefault(d.evaluation_audit_id, []).append({
            "dimension": d.dimension_id,
            "score":     d.score,
            "status":    d.status,
            "provider":  d.provider,
        })

    return [
        {
            "interaction_id":    a.interaction_id,
            "app_id":            a.app_id,
            "session_id":        a.session_id,
            "verdict":           a.verdict,
            "dimension_results": dims_by_id.get(a.id, []),
        }
        for a in audit_rows
    ]


# ── Score computation ──────────────────────────────────────────────────────────

def _compute_trust_score(
    records: list[dict],
    weights: dict[str, float],
) -> dict:
    """
    Compute per-category scores and the 0-100 composite trust score.

    Two changes from the naive formula:
      1. Categories with no evaluations in the window are excluded from the composite
         (their weight is redistributed to observed categories via denominator normalisation).
      2. A volume-independent COERCIVE penalty deduction is applied after the weighted average
         so that a handful of dangerous interactions visibly lower the score regardless of
         how much clean traffic surrounds them.

    Returns a dict suitable for direct JSON serialisation.
    """
    if not records:
        return {
            "total_evals":      0,
            "categories":       {cat: {"score": None, "sample": 0} for cat in _CATEGORY_DIMS},
            "weights_used":     {},
            "composite":        None,
            "coercive_count":   0,
            "coercive_penalty": 0.0,
            "trust_score":      None,
            "status":           "no_data",
        }

    # ── Step 1: build per-category normalised score lists ──────────────────────
    cat_scores: dict[str, list[float]] = {cat: [] for cat in _CATEGORY_DIMS}

    for rec in records:
        dim_results = rec.get("dimension_results") or []
        dim_map: dict[str, int] = {}
        for dr in dim_results:
            dim   = dr.get("dimension", "")
            score = dr.get("score")
            if dim and score is not None:
                dim_map[dim] = int(score)

        for cat, dims in _CATEGORY_DIMS.items():
            scores_in_cat = [dim_map[d] for d in dims if d in dim_map]
            if scores_in_cat:
                worst      = max(scores_in_cat)
                # Normalise: score 1 (safe) → 1.0,  score 9 (critical) → 0.0
                normalized = 1.0 - (worst - 1) / 8.0
                cat_scores[cat].append(normalized)

    def _avg(lst: list) -> Optional[float]:
        return round(sum(lst) / len(lst), 4) if lst else None

    cat_avgs: dict[str, Optional[float]] = {
        cat: _avg(cat_scores[cat]) for cat in _CATEGORY_DIMS
    }

    # ── Step 2: weighted composite — exclude categories with no data ───────────
    # Missing categories are skipped entirely; their weight is NOT added to
    # total_weight, so the remaining weights auto-renormalise to sum to 1.0.
    total_weight = 0.0
    weighted_sum = 0.0
    for cat, w in weights.items():
        eff_score = cat_avgs.get(cat)
        if eff_score is None:
            # No evaluations for this category in the window — exclude entirely.
            # This avoids both the "silent free 1.0" and the harsh "0.0 penalty"
            # for areas the app simply hasn't been evaluated against yet.
            continue
        weighted_sum += w * eff_score
        total_weight += w

    composite = round(weighted_sum / total_weight, 4) if total_weight > 0 else 1.0

    # ── Step 3: COERCIVE penalty — volume-independent deduction ───────────────
    # Count how many records the PE explicitly flagged as COERCIVE.  Each one
    # deducts _COERCIVE_PENALTY_PER_EVENT from the composite (0-1 scale),
    # regardless of how much clean traffic surrounds it.
    coercive_count   = sum(
        1 for r in records
        if (r.get("verdict") or "").upper() == "COERCIVE"
    )
    coercive_penalty = round(
        min(_COERCIVE_PENALTY_CAP, coercive_count * _COERCIVE_PENALTY_PER_EVENT),
        4,
    )
    adjusted_composite = max(0.0, composite - coercive_penalty)
    trust_score        = round(adjusted_composite * 100)

    logger.debug(
        "[trust-score] composite=%.4f coercive=%d penalty=%.4f → trust_score=%d",
        composite, coercive_count, coercive_penalty, trust_score,
    )

    # Build per-category detail including which dimensions were observed
    # Pre-compute the set of dimension ids present in this batch for O(1) lookup
    seen_dims: set[str] = {
        dr.get("dimension", "")
        for rec in records
        for dr in (rec.get("dimension_results") or [])
    }
    cat_detail: dict[str, dict] = {}
    for cat in _CATEGORY_DIMS:
        observed_dims = sorted(_CATEGORY_DIMS[cat] & seen_dims)
        cat_detail[cat] = {
            "score":         cat_avgs[cat],
            "sample":        len(cat_scores[cat]),
            "dims_observed": observed_dims,
        }

    return {
        "total_evals": len(records),
        "categories":  cat_detail,
        "weights_used": {                           # actual weights that contributed
            cat: round(w / total_weight, 4)
            for cat, w in weights.items()
            if cat_avgs.get(cat) is not None and total_weight > 0
        },
        "composite":        composite,        # pre-penalty weighted average (0-1)
        "coercive_count":   coercive_count,   # number of COERCIVE events in window
        "coercive_penalty": coercive_penalty, # pp deducted by COERCIVE penalty (0-1)
        "trust_score":      trust_score,      # final 0-100 score (penalty applied)
        "status":           "available",
    }


# ── Weight loader ──────────────────────────────────────────────────────────────

def _load_weights(db: Session, app_id: Optional[str]) -> dict[str, float]:
    """Load weights from config_app_trust_weights (app-specific -> enterprise default -> hard-coded)."""
    row = None
    if app_id:
        row = db.query(AppTrustWeights).filter_by(app_id=app_id, active=True).first()
    if row is None:
        row = db.query(AppTrustWeights).filter_by(app_id=None, active=True).first()
    if row is None:
        return _DEFAULT_WEIGHTS.copy()
    weights = {
        "safety":      row.weight_safety,
        "security":    row.weight_security,
        "compliance":  row.weight_compliance,
        "operational": row.weight_operational,
        "quality":     row.weight_quality,
    }
    # Guard: weights must sum to 1.0 (±0.01 tolerance for floating-point rounding).
    # If the DB row is misconfigured, fall back to hard-coded defaults and log a warning.
    total = sum(weights.values())
    if abs(total - 1.0) > 0.01:
        logger.warning(
            "[trust_score] Weights for app_id=%s sum to %.4f (expected 1.0). "
            "Falling back to default weights.",
            app_id, total,
        )
        return _DEFAULT_WEIGHTS.copy()
    return weights


# ── Snapshot writer ────────────────────────────────────────────────────────────

def _write_snapshot(
    db: Session,
    app_id: Optional[str],
    window: str,
    window_start: datetime,
    result: dict,
) -> None:
    """Persist a trust score point to metric_snapshots. No-op when trust_score is None."""
    if result.get("trust_score") is None:
        return
    snap = MetricSnapshot(
        metric_name="app_trust_score",
        labels={"app_id": app_id or "all", "window": window},
        value=float(result["trust_score"]),
        window_type=window,
        window_start=window_start.replace(tzinfo=None),
        computed_at=datetime.utcnow(),
    )
    db.add(snap)
    db.commit()
    logger.debug(
        f"[trust-score] Snapshot written: app={app_id!r} window={window} "
        f"score={result['trust_score']}"
    )


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("")
def get_trust_score(
    app_id:  Optional[str] = Query(None, description="Filter to a specific app. Omit for all apps + platform aggregate."),
    window:  str            = Query("24h", description="Time window: 1h | 6h | 24h | 7d | 30d"),
    persist: bool           = Query(True,  description="Write computed score to metric_snapshots for trend tracking"),
    db: Session             = Depends(get_db),
):
    """
    Compute the App Trust Score (0-100) from audit evaluation data.

    The score is a weighted composite of five category pass rates.
    Evaluations from the specified time window are used.
    Data is read from audit_evaluation_audit + audit_evaluation_dimensions (SQLite).
    """
    since = _window_start(window)

    if app_id:
        records = _read_records(since, app_id=app_id, db=db)
        weights = _load_weights(db, app_id)
        result  = _compute_trust_score(records, weights)
        if persist:
            _write_snapshot(db, app_id, window, since, result)
        return {
            "window":  window,
            "since":   since.isoformat(),
            "app_id":  app_id,
            "weights": weights,
            **result,
        }

    # All apps
    all_records = _read_records(since, app_id=None, db=db)
    seen_apps   = sorted({r.get("app_id") for r in all_records if r.get("app_id")})

    # Platform-wide aggregate
    plat_weights = _load_weights(db, app_id=None)
    platform     = _compute_trust_score(all_records, plat_weights)
    if persist:
        _write_snapshot(db, None, window, since, platform)

    # Per-app scores
    per_app = []
    for aid in seen_apps:
        app_recs = [r for r in all_records if r.get("app_id") == aid]
        w        = _load_weights(db, aid)
        res      = _compute_trust_score(app_recs, w)
        if persist:
            _write_snapshot(db, aid, window, since, res)
        per_app.append({"app_id": aid, "weights": w, **res})

    return {
        "window":   window,
        "since":    since.isoformat(),
        "platform": {"weights": plat_weights, **platform},
        "apps":     per_app,
    }


@router.get("/history")
def get_trust_score_history(
    app_id: Optional[str] = Query(None, description="Filter to a specific app (use 'all' for platform aggregate)"),
    window: str            = Query("24h", description="Window type to retrieve history for"),
    limit:  int            = Query(50,    ge=1, le=500),
    db: Session            = Depends(get_db),
):
    """
    Return recent metric_snapshots rows for trend / sparkline rendering.
    Ordered oldest-first so the UI can plot them as a time series directly.
    """
    target_app = app_id or "all"

    candidates = (
        db.query(MetricSnapshot)
        .filter(
            MetricSnapshot.metric_name == "app_trust_score",
            MetricSnapshot.window_type == window,
        )
        .order_by(MetricSnapshot.computed_at.desc())
        .limit(limit * 4)
        .all()
    )

    filtered = [
        s for s in candidates
        if (s.labels or {}).get("app_id") == target_app
    ][:limit]

    filtered.sort(key=lambda s: s.computed_at)

    return {
        "app_id":    target_app,
        "window":    window,
        "count":     len(filtered),
        "snapshots": [s.to_dict() for s in filtered],
    }
