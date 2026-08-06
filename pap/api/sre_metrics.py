"""
SRE Metrics API — Burn-rate alerting for platform health indicators.

Tracks two key SRE metrics using the Google SRE two-window burn-rate pattern:
an alert fires only when BOTH the fast window (1h) AND the medium window (6h)
exceed their thresholds simultaneously.  A single noisy spike in 1h alone does
not fire — sustained degradation across both windows is required.

Metrics tracked:
  1. Sensitive data disclosure rate
     — sensitive_data_disclosure violations / total evals
     — Alert: 1h rate > 5% AND 6h rate > 3%

  2. Governance latency p95 (SYNC evals only)
     — PE evaluation overhead; ASYNC evals excluded as they are off the critical path
     — Alert: 1h p95 > 500 ms AND 6h p95 > 400 ms

Alerts are written to trust_score_alerts (event lifecycle table) with status=open.
Duplicate suppression: no new alert is created if an open alert of the same type
was triggered within the last hour for the same app.

Also writes per-window metric snapshots to metric_snapshots on every request
so the trend data is available for dashboard sparklines.

Endpoints:
  GET  /v1/sre/metrics          — current metric rates for 1h, 6h, 24h windows
  GET  /v1/sre/alerts           — list trust_score_alerts
  POST /v1/sre/alerts/{id}/ack  — acknowledge an open alert
  POST /v1/sre/alerts/{id}/resolve — resolve an alert
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.database import get_db
from core.models import MetricSnapshot, TrustScoreAlert

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/sre", tags=["SRE Metrics"])

# ── Burn-rate alert thresholds ─────────────────────────────────────────────────
#
# Policy rationale (Google SRE two-window burn-rate pattern):
#   An alert fires ONLY when BOTH the fast (1h) AND medium (6h) window breach
#   their threshold simultaneously.  A spike in the 1h window alone is noise;
#   sustained degradation across both windows signals a real incident.
#
# PII exposure rate thresholds
#   1h > 5%:  More than 1-in-20 evals leaking PII in the last hour is an
#             active incident — requires immediate investigation.
#   6h > 3%:  Sustained rate above 3% over 6h indicates a systemic issue
#             (mis-configured policy, new attack pattern, or SDK regression).
#   Rationale: Banking-grade apps should target <0.5% in steady state.
#   These thresholds are intentionally conservative for demo/staging traffic
#   where synthetic test scenarios include deliberate PII violations.
#   Tighten to 1h > 1% / 6h > 0.5% for production.
_PII_THRESHOLD_1H  = 0.05    # 5%  — fast-window breach trigger
_PII_THRESHOLD_6H  = 0.03    # 3%  — medium-window sustained breach trigger

# Governance latency p95 thresholds (SYNC evals only — on user request critical path)
#   1h p95 > 500ms:  Evaluation overhead exceeding 500ms p95 for an hour means
#                    users are seeing >0.5s added latency on every blocked request.
#   6h p95 > 400ms:  Sustained median overhead above 400ms indicates PE under-provisioning
#                    or a hot-path regression in the guardrail service.
#   Rationale: PE target SLO is p95 < 300ms.  Thresholds are set at 1.5× (500ms)
#   and 1.3× (400ms) of the target to provide headroom before alerting.
_LATENCY_THRESHOLD_1H_MS = 500.0   # ms — fast-window p95 breach trigger
_LATENCY_THRESHOLD_6H_MS = 400.0   # ms — medium-window p95 sustained breach trigger

# Duplicate suppression: suppress a new alert if one of the same type is already
# open and was triggered within this window.
_DEDUP_WINDOW = timedelta(hours=1)

# ── Window helpers ─────────────────────────────────────────────────────────────

_WINDOWS: dict[str, timedelta] = {
    "1h":  timedelta(hours=1),
    "6h":  timedelta(hours=6),
    "24h": timedelta(hours=24),
}


def _since(window: str) -> datetime:
    return datetime.now(timezone.utc) - _WINDOWS[window]


# ── SQLite reader ──────────────────────────────────────────────────────────────

def _read_window(since: datetime, app_id: Optional[str], db: Session) -> list[dict]:
    """
    Read all audit records after `since` from SQLite, joined with dimension scores.

    Returns a list of dicts with the fields used by _pii_rate() and
    _latency_stats() — structurally identical to what the old JSONL reader
    returned so all compute logic is unchanged.
    """
    since_str  = since.replace(tzinfo=None).isoformat(timespec="seconds")
    app_filter = "AND a.app_id = :app_id" if app_id else ""
    params: dict = {"since": since_str}
    if app_id:
        params["app_id"] = app_id

    audit_sql = text(f"""
        SELECT a.id, a.app_id, a.verdict, a.latency_ms
        FROM audit_evaluation_audit a
        WHERE a.timestamp >= :since
          {app_filter}
        ORDER BY a.timestamp DESC
        LIMIT 10000
    """)

    dim_sql = text(f"""
        SELECT d.evaluation_audit_id, d.dimension_id, d.score, d.status, d.provider
        FROM audit_evaluation_dimensions d
        JOIN audit_evaluation_audit a ON a.id = d.evaluation_audit_id
        WHERE a.timestamp >= :since
          {app_filter}
    """)

    audit_rows = db.execute(audit_sql, params).fetchall()
    dim_rows   = db.execute(dim_sql,   params).fetchall()

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
            "app_id":            a.app_id,
            "verdict":           a.verdict,
            "latency_ms":        a.latency_ms,
            # eval_mode absent -> defaults to "SYNC" in _latency_stats (conservative)
            "dimension_results": dims_by_id.get(a.id, []),
        }
        for a in audit_rows
    ]


# ── PII exposure rate ──────────────────────────────────────────────────────────

def _pii_rate(records: list[dict]) -> dict:
    """
    Compute the PII exposure rate for a set of audit records.

    A record is counted as a sensitive data disclosure when:
      - verdict is non-PERMISSIVE  (policy was active and triggered)
      - sensitive_data_disclosure dimension scored ≥ 4  (NORMATIVE or COERCIVE threshold)

    Returns: {flagged, total, rate (0-1), rate_pct}
    """
    total   = len(records)
    flagged = sum(
        1 for r in records
        if r.get("verdict") != "PERMISSIVE"
        and any(
            d.get("dimension") == "sensitive_data_disclosure" and (d.get("score") or 0) >= 4
            for d in (r.get("dimension_results") or [])
        )
    )
    rate = round(flagged / total, 4) if total else 0.0
    return {
        "flagged":   flagged,
        "total":     total,
        "rate":      rate,
        "rate_pct":  round(rate * 100, 2),
    }


# ── Governance latency ─────────────────────────────────────────────────────────

def _latency_stats(records: list[dict]) -> dict:
    """
    Compute latency statistics for SYNC evaluations only.

    eval_mode discrimination:
      SYNC  — evaluation happened on the user request critical path → counts toward SLO
      ASYNC — evaluation happened out-of-band → excluded from latency SLOs
      absent — records without eval_mode are treated as SYNC (conservative)

    Returns: {p50, p95, avg, sample, note}
    """
    sync_latencies = sorted(
        r["latency_ms"]
        for r in records
        if r.get("latency_ms")
        and r.get("eval_mode", "SYNC") != "ASYNC"
    )
    n = len(sync_latencies)
    if n == 0:
        return {
            "p50":    None,
            "p95":    None,
            "avg":    None,
            "sample": 0,
            "note":   "No SYNC eval data in this window",
        }

    p50_idx = max(0, math.ceil(n * 0.50) - 1)
    p95_idx = max(0, math.ceil(n * 0.95) - 1)
    return {
        "p50":    round(sync_latencies[p50_idx], 1),
        "p95":    round(sync_latencies[p95_idx], 1),
        "avg":    round(sum(sync_latencies) / n, 1),
        "sample": n,
        "note":   "SYNC evals only (ASYNC evals excluded - not on user request critical path)",
    }


# ── Metric snapshot writer ─────────────────────────────────────────────────────

def _write_metric_snapshots(
    db: Session,
    app_id: Optional[str],
    window: str,
    since: datetime,
    pii: dict,
    lat: dict,
) -> None:
    """Write PII rate and latency p95 to metric_snapshots for trend tracking."""
    now = datetime.utcnow()
    labels = {"app_id": app_id or "all", "window": window}
    window_start = since.replace(tzinfo=None)

    snaps = [
        MetricSnapshot(
            metric_name="pii_exposure_rate",
            labels=labels,
            value=pii["rate"],
            window_type=window,
            window_start=window_start,
            computed_at=now,
        ),
    ]
    if lat.get("p95") is not None:
        snaps.append(MetricSnapshot(
            metric_name="latency_p95_ms",
            labels={**labels, "eval_mode": "SYNC"},
            value=lat["p95"],
            window_type=window,
            window_start=window_start,
            computed_at=now,
        ))

    for s in snaps:
        db.add(s)
    db.commit()


# ── Burn-rate check & alert creation ──────────────────────────────────────────

def _check_and_alert(
    db: Session,
    app_id: Optional[str],
    pii_1h: dict,
    pii_6h: dict,
    lat_1h: dict,
    lat_6h: dict,
) -> list[dict]:
    """
    Apply two-window burn-rate conditions. Write alerts if thresholds exceeded.

    Deduplication: skip if an open alert of the same type for this app was
    triggered within _DEDUP_WINDOW (1h) — avoids alert storms during sustained
    incidents.

    Returns list of newly created alert dicts.
    """
    now        = datetime.utcnow()
    dedup_from = now - _DEDUP_WINDOW
    new_alerts: list[dict] = []

    def _has_open_alert(alert_type: str) -> bool:
        return (
            db.query(TrustScoreAlert)
            .filter(
                TrustScoreAlert.app_id     == app_id,
                TrustScoreAlert.alert_type == alert_type,
                TrustScoreAlert.status     == "open",
                TrustScoreAlert.triggered_at >= dedup_from,
            )
            .first()
        ) is not None

    # ── PII burn-rate ──────────────────────────────────────────────────────────
    pii_triggered = (
        pii_1h["total"] > 0
        and pii_6h["total"] > 0
        and pii_1h["rate"] > _PII_THRESHOLD_1H
        and pii_6h["rate"] > _PII_THRESHOLD_6H
    )
    if pii_triggered and not _has_open_alert("pii_burn_rate"):
        severity = "CRITICAL" if pii_1h["rate"] > 0.20 else ("HIGH" if pii_1h["rate"] > 0.10 else "MEDIUM")
        alert = TrustScoreAlert(
            app_id=app_id,
            alert_type="pii_burn_rate",
            severity=severity,
            fast_window="1h",
            medium_window="6h",
            fast_burn_rate=pii_1h["rate"],
            medium_burn_rate=pii_6h["rate"],
            dimension_id="sensitive_data_disclosure",
            status="open",
            triggered_at=now,
            notes=(
                f"PII exposure rate exceeded both burn-rate thresholds: "
                f"1h={pii_1h['rate_pct']}% (threshold {_PII_THRESHOLD_1H * 100:.0f}%), "
                f"6h={pii_6h['rate_pct']}% (threshold {_PII_THRESHOLD_6H * 100:.0f}%). "
                f"Based on {pii_6h['total']} evals in 6h window."
            ),
        )
        db.add(alert)
        db.flush()
        new_alerts.append(alert.to_dict())
        logger.warning(
            f"[SRE] PII burn-rate alert created for app={app_id!r}: "
            f"1h={pii_1h['rate_pct']}%, 6h={pii_6h['rate_pct']}%"
        )

    # ── Latency burn-rate ──────────────────────────────────────────────────────
    p95_1h = lat_1h.get("p95")
    p95_6h = lat_6h.get("p95")
    lat_triggered = (
        p95_1h is not None
        and p95_6h is not None
        and p95_1h > _LATENCY_THRESHOLD_1H_MS
        and p95_6h > _LATENCY_THRESHOLD_6H_MS
    )
    if lat_triggered and not _has_open_alert("latency_burn_rate"):
        alert = TrustScoreAlert(
            app_id=app_id,
            alert_type="latency_burn_rate",
            severity="HIGH" if p95_1h > 1000 else "MEDIUM",
            fast_window="1h",
            medium_window="6h",
            fast_burn_rate=p95_1h,      # stored as ms value in the burn_rate field
            medium_burn_rate=p95_6h,
            dimension_id=None,
            status="open",
            triggered_at=now,
            notes=(
                f"Governance latency p95 exceeded both burn-rate thresholds: "
                f"1h={p95_1h}ms (threshold {_LATENCY_THRESHOLD_1H_MS:.0f}ms), "
                f"6h={p95_6h}ms (threshold {_LATENCY_THRESHOLD_6H_MS:.0f}ms). "
                f"SYNC evals only ({lat_6h['sample']} samples in 6h window)."
            ),
        )
        db.add(alert)
        db.flush()
        new_alerts.append(alert.to_dict())
        logger.warning(
            f"[SRE] Latency burn-rate alert created for app={app_id!r}: "
            f"p95 1h={p95_1h}ms, 6h={p95_6h}ms"
        )

    if new_alerts:
        db.commit()

    return new_alerts


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("/metrics")
def get_sre_metrics(
    app_id:       Optional[str] = Query(None, description="Filter to a specific app. Omit for platform-wide."),
    check_alerts: bool           = Query(True, description="Run burn-rate check and write alerts to trust_score_alerts if thresholds are exceeded"),
    persist:      bool           = Query(True, description="Write metric values to metric_snapshots for trend tracking"),
    db: Session                  = Depends(get_db),
):
    """
    Return SRE health metrics for 1h, 6h, and 24h windows.

    Metrics returned:
      pii_exposure_rate    — fraction of evals with data_leakage violations
      governance_latency   — p95 governance latency for SYNC evals

    Burn-rate alert conditions (Google SRE two-window pattern):
      PII:     1h rate > 5%    AND 6h rate > 3%
      Latency: 1h p95 > 500ms AND 6h p95 > 400ms

    When check_alerts=true (default), new alerts are written to trust_score_alerts
    if the above conditions are met and no open alert exists in the last hour.
    """
    # Load records for each window from SQLite
    records_1h  = _read_window(_since("1h"),  app_id, db)
    records_6h  = _read_window(_since("6h"),  app_id, db)
    records_24h = _read_window(_since("24h"), app_id, db)

    pii_1h  = _pii_rate(records_1h)
    pii_6h  = _pii_rate(records_6h)
    pii_24h = _pii_rate(records_24h)

    lat_1h  = _latency_stats(records_1h)
    lat_6h  = _latency_stats(records_6h)
    lat_24h = _latency_stats(records_24h)

    # Burn-rate alert conditions
    pii_alert = (
        pii_1h["total"] > 0
        and pii_6h["total"] > 0
        and pii_1h["rate"] > _PII_THRESHOLD_1H
        and pii_6h["rate"] > _PII_THRESHOLD_6H
    )
    p95_1h = lat_1h.get("p95")
    p95_6h = lat_6h.get("p95")
    lat_alert = (
        p95_1h is not None
        and p95_6h is not None
        and p95_1h > _LATENCY_THRESHOLD_1H_MS
        and p95_6h > _LATENCY_THRESHOLD_6H_MS
    )

    # Persist snapshots
    if persist:
        for window, pii, lat, recs in [
            ("1h",  pii_1h,  lat_1h,  records_1h),
            ("6h",  pii_6h,  lat_6h,  records_6h),
            ("24h", pii_24h, lat_24h, records_24h),
        ]:
            if recs:
                _write_metric_snapshots(db, app_id, window, _since(window), pii, lat)

    # Check and create alerts
    new_alerts: list[dict] = []
    if check_alerts and (pii_alert or lat_alert):
        new_alerts = _check_and_alert(db, app_id, pii_1h, pii_6h, lat_1h, lat_6h)

    return {
        "app_id":       app_id or "all",
        "computed_at":  datetime.utcnow().isoformat(),
        "thresholds": {
            "pii_rate_1h":        _PII_THRESHOLD_1H,
            "pii_rate_6h":        _PII_THRESHOLD_6H,
            "latency_p95_1h_ms":  _LATENCY_THRESHOLD_1H_MS,
            "latency_p95_6h_ms":  _LATENCY_THRESHOLD_6H_MS,
        },
        "pii_exposure_rate": {
            "1h":  {**pii_1h,  "threshold": _PII_THRESHOLD_1H, "breached": pii_1h["rate"] > _PII_THRESHOLD_1H},
            "6h":  {**pii_6h,  "threshold": _PII_THRESHOLD_6H, "breached": pii_6h["rate"] > _PII_THRESHOLD_6H},
            "24h": {**pii_24h, "threshold": _PII_THRESHOLD_6H, "breached": pii_24h["rate"] > _PII_THRESHOLD_6H},
            "burn_rate_alert_active": pii_alert,
            "note": "sensitive_data_disclosure dimension, score >= 4, verdict non-PERMISSIVE",
        },
        "governance_latency": {
            "1h":  {**lat_1h,  "threshold_ms": _LATENCY_THRESHOLD_1H_MS, "breached": (p95_1h or 0) > _LATENCY_THRESHOLD_1H_MS},
            "6h":  {**lat_6h,  "threshold_ms": _LATENCY_THRESHOLD_6H_MS, "breached": (p95_6h or 0) > _LATENCY_THRESHOLD_6H_MS},
            "24h": {**lat_24h, "threshold_ms": _LATENCY_THRESHOLD_6H_MS, "breached": (lat_24h.get("p95") or 0) > _LATENCY_THRESHOLD_6H_MS},
            "burn_rate_alert_active": lat_alert,
            "eval_mode_note": "SYNC evals only - ASYNC evals are excluded from latency SLOs (not on user request critical path)",
        },
        "alerts_created": new_alerts,
    }


@router.get("/timeseries")
def get_timeseries(
    window: str          = Query("24h", description="Overall window: 1h | 24h | 7d"),
    app_id: Optional[str] = Query(None, description="Filter to a specific app"),
    db: Session           = Depends(get_db),
):
    """
    Adaptive time-series data for dashboard charts.

    Bucket sizes adapt automatically to the requested window:
      window=1h   → 12 × 5-minute buckets
      window=24h  → 24 × 1-hour buckets
      window=7d   →  7 × 1-day  buckets

    Response shape:
      {
        "window":       "24h",
        "bucket_size":  "1h",
        "bucket_count": 24,
        "buckets": [
          {
            "ts":           "2026-05-03T12:00:00",   // UTC bucket start (ISO)
            "label":        "12:00",                   // short display label
            "eval_count":   145,
            "block_count":  3,
            "warn_count":   8,
            "pass_count":   134,
            "block_rate_pct": 2.07,
            "pii_rate_pct": 0.69,
            "latency_p95_ms": null  // null if insufficient samples
          },
          ...
        ]
      }

    All buckets in the requested window are returned, even if empty (count=0).
    This lets the UI render a complete x-axis without gaps.
    """
    # ── Determine window parameters ──────────────────────────────────────────
    _WINDOW_CONFIGS = {
        "1h":  {"delta": timedelta(hours=1),  "bucket_secs": 300,       "bucket_size": "5m",  "n_buckets": 12,  "label_fmt": "%H:%M"},
        "24h": {"delta": timedelta(hours=24), "bucket_secs": 3600,      "bucket_size": "1h",  "n_buckets": 24,  "label_fmt": "%H:%M"},
        "7d":  {"delta": timedelta(days=7),   "bucket_secs": 86400,     "bucket_size": "1d",  "n_buckets": 7,   "label_fmt": "%a"},
    }

    cfg = _WINDOW_CONFIGS.get(window)
    if not cfg:
        cfg = _WINDOW_CONFIGS["24h"]
        window = "24h"

    now         = datetime.now(timezone.utc)
    window_start = now - cfg["delta"]
    bucket_secs  = cfg["bucket_secs"]

    # ── Fetch audit rows in the window ────────────────────────────────────────
    since_str = window_start.replace(tzinfo=None).isoformat(timespec="seconds")
    app_filter = "AND a.app_id = :app_id" if app_id else ""
    params: dict = {"since": since_str}
    if app_id:
        params["app_id"] = app_id

    audit_sql = text(f"""
        SELECT a.id, a.verdict, a.latency_ms,
               strftime('%s', a.timestamp) AS ts_epoch
        FROM audit_evaluation_audit a
        WHERE a.timestamp >= :since
          {app_filter}
    """)

    dim_sql = text(f"""
        SELECT d.evaluation_audit_id, d.dimension_id, d.score
        FROM audit_evaluation_dimensions d
        JOIN audit_evaluation_audit a ON a.id = d.evaluation_audit_id
        WHERE a.timestamp >= :since
          {app_filter}
          AND d.dimension_id = 'sensitive_data_disclosure'
    """)

    audit_rows = db.execute(audit_sql, params).fetchall()
    dim_rows   = db.execute(dim_sql,   params).fetchall()

    # Build sensitive_data_disclosure score lookup by evaluation id
    leakage_scores: dict[int, int] = {}
    for d in dim_rows:
        leakage_scores[d.evaluation_audit_id] = d.score or 0

    # ── Build bucket dictionary ───────────────────────────────────────────────
    # Align bucket starts to epoch boundaries (so 1h buckets start on the hour)
    window_epoch = int(window_start.timestamp())
    n_buckets    = cfg["n_buckets"]

    # Pre-fill all buckets (empty)
    buckets: dict[int, dict] = {}
    for i in range(n_buckets):
        bstart = window_epoch + i * bucket_secs
        buckets[bstart] = {
            "ts_epoch":   bstart,
            "eval_count":  0,
            "block_count": 0,
            "warn_count":  0,
            "pass_count":  0,
            "pii_flagged": 0,
            "latencies":   [],
        }

    # Assign each audit row to its bucket
    for row in audit_rows:
        if row.ts_epoch is None:
            continue
        epoch = int(row.ts_epoch)
        # Snap to bucket start: floor to nearest bucket_secs boundary
        offset     = epoch - window_epoch
        bucket_idx = max(0, min(n_buckets - 1, offset // bucket_secs))
        bstart     = window_epoch + bucket_idx * bucket_secs
        bkt        = buckets.get(bstart)
        if bkt is None:
            continue

        bkt["eval_count"] += 1

        verdict = (row.verdict or "").upper()
        if verdict == "COERCIVE":
            bkt["block_count"] += 1
        elif verdict == "NORMATIVE":
            bkt["warn_count"] += 1
        else:
            bkt["pass_count"] += 1

        # PII exposure: data_leakage score >= 4 in non-PERMISSIVE context
        if verdict != "PERMISSIVE" and leakage_scores.get(row.id, 0) >= 4:
            bkt["pii_flagged"] += 1

        if row.latency_ms:
            bkt["latencies"].append(row.latency_ms)

    # ── Serialise buckets ─────────────────────────────────────────────────────
    import time as _time
    label_fmt = cfg["label_fmt"]
    result_buckets = []
    for bstart in sorted(buckets.keys()):
        bkt = buckets[bstart]
        n   = bkt["eval_count"]

        # p95 latency in bucket
        lats    = sorted(bkt["latencies"])
        p95_lat = None
        if lats:
            idx     = max(0, int(len(lats) * 0.95) - 1)
            p95_lat = round(lats[idx], 1)

        block_rate = round(bkt["block_count"] / n * 100, 1) if n else 0.0
        pii_rate   = round(bkt["pii_flagged"] / n * 100, 1) if n else 0.0

        # Generate label
        import datetime as _dt
        ts_utc = _dt.datetime.utcfromtimestamp(bstart)
        label  = ts_utc.strftime(label_fmt)
        ts_iso = ts_utc.strftime("%Y-%m-%dT%H:%M:%S")

        result_buckets.append({
            "ts":             ts_iso,
            "label":          label,
            "eval_count":     n,
            "block_count":    bkt["block_count"],
            "warn_count":     bkt["warn_count"],
            "pass_count":     bkt["pass_count"],
            "block_rate_pct": block_rate,
            "pii_rate_pct":   pii_rate,
            "latency_p95_ms": p95_lat,
        })

    total_evals = sum(b["eval_count"] for b in result_buckets)

    return {
        "window":        window,
        "bucket_size":   cfg["bucket_size"],
        "bucket_count":  n_buckets,
        "total_evals":   total_evals,
        "app_id":        app_id or "all",
        "buckets":       result_buckets,
    }


@router.get("/alerts")
def list_alerts(
    app_id: Optional[str] = Query(None, description="Filter to a specific app"),
    status: Optional[str] = Query(None, description="Filter by status: open | acknowledged | resolved | suppressed"),
    limit:  int            = Query(50, ge=1, le=500),
    db: Session            = Depends(get_db),
):
    """List trust_score_alerts, newest first."""
    q = db.query(TrustScoreAlert)
    if app_id:
        q = q.filter(TrustScoreAlert.app_id == app_id)
    if status:
        q = q.filter(TrustScoreAlert.status == status.lower())
    total  = q.count()
    alerts = q.order_by(TrustScoreAlert.triggered_at.desc()).limit(limit).all()
    return {
        "total":  total,
        "limit":  limit,
        "alerts": [a.to_dict() for a in alerts],
    }


@router.post("/alerts/{alert_id}/ack")
def acknowledge_alert(
    alert_id: int,
    db: Session = Depends(get_db),
):
    """
    Acknowledge an open alert.

    Transitions status: open → acknowledged.
    Use this to indicate the on-call engineer is aware and investigating.
    """
    alert = db.query(TrustScoreAlert).filter(TrustScoreAlert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")
    if alert.status != "open":
        raise HTTPException(
            status_code=409,
            detail=f"Alert is already '{alert.status}' — can only acknowledge 'open' alerts",
        )
    alert.status          = "acknowledged"
    alert.acknowledged_at = datetime.utcnow()
    db.commit()
    db.refresh(alert)
    logger.info(f"[SRE] Alert {alert_id} acknowledged (type={alert.alert_type}, app={alert.app_id!r})")
    return alert.to_dict()


@router.post("/alerts/{alert_id}/resolve")
def resolve_alert(
    alert_id: int,
    db: Session = Depends(get_db),
):
    """
    Resolve an alert.

    Transitions status: open | acknowledged → resolved.
    Use this once the underlying issue is confirmed fixed.
    """
    alert = db.query(TrustScoreAlert).filter(TrustScoreAlert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")
    if alert.status == "resolved":
        raise HTTPException(status_code=409, detail="Alert is already resolved")
    alert.status      = "resolved"
    alert.resolved_at = datetime.utcnow()
    db.commit()
    db.refresh(alert)
    logger.info(f"[SRE] Alert {alert_id} resolved (type={alert.alert_type}, app={alert.app_id!r})")
    return alert.to_dict()
