"""
Rolling retention for the high-growth audit + metrics tables (opt-in).

Why: under sustained traffic the eval-trail (`audit_evaluation_audit` +
`audit_evaluation_dimensions` ~10x rows + `audit_interaction_events`) and the
metric snapshots (`metric_snapshots`, ~9k rows/day) grow unbounded. This module
runs a periodic in-process sweep that DELETES rows older than a configured number
of days, keeping a rolling window (e.g. the last 1 day) so the DB stays bounded.

Enabled entirely by environment — same behaviour locally (start_all → pap/.env)
and in the container (cloudbuild → Cloud Run env):

    PAP_RETENTION_DAYS           master switch + default window in days (0/unset = OFF)
    PAP_RETENTION_AUDIT_DAYS     override for the eval-trail tables  (default: PAP_RETENTION_DAYS)
    PAP_RETENTION_METRICS_DAYS   override for the snapshot tables    (default: PAP_RETENTION_DAYS)
    PAP_RETENTION_INTERVAL_MIN   sweep interval in minutes           (default: 60)

NOTE — TIMESTAMP FORMATS DIFFER PER TABLE: audit_evaluation_audit uses
'YYYY-MM-DDTHH:MM:SS.sss' (T, naive UTC), audit_interaction_events appends
'+00:00', and metric_snapshots uses 'YYYY-MM-DD HH:MM:SS.ssssss' (space). A naive
string-window comparison would silently mis-prune. We compare with SQLite's
julianday(), which parses ALL of those forms and 'now' as UTC — so the cutoff is
correct regardless of the per-column format.

CAUTION — pruning audit_evaluation_audit shifts per-agent trust scores: the Agent
Cards score is computed live from audit verdicts, so removing old (e.g. COERCIVE)
rows RAISES scores. Keep PAP_RETENTION_AUDIT_DAYS longer than the metrics window if
you want the demo's trust scores stable while still bounding the bigger snapshot
bloat.

We do NOT VACUUM: reclaimed pages go on SQLite's freelist and are reused by new
inserts, so steady-state file size stays bounded without taking the exclusive lock
that VACUUM needs (which would stall the live dashboard). VACUUM offline if you want
to shrink the file on disk.
"""
import asyncio
import logging
import os

from sqlalchemy import text

from core.database import engine, DATABASE_URL

logger = logging.getLogger("pap.retention")

# Eval-trail tables, pruned together by the audit window. Dimensions has NO
# timestamp column, so it is keyed off its parent audit row and MUST be deleted
# first (before the parent rows it references are removed).
_AUDIT_DELETES = [
    ("audit_evaluation_dimensions",
     "DELETE FROM audit_evaluation_dimensions WHERE evaluation_audit_id IN "
     "(SELECT id FROM audit_evaluation_audit WHERE julianday(timestamp) < julianday('now', :mod))"),
    ("audit_evaluation_audit",
     "DELETE FROM audit_evaluation_audit WHERE julianday(timestamp) < julianday('now', :mod)"),
    ("audit_interaction_events",
     "DELETE FROM audit_interaction_events WHERE julianday(timestamp) < julianday('now', :mod)"),
]
# Snapshot tables, pruned together by the metrics window (by their write time).
_METRIC_DELETES = [
    ("metric_snapshots",
     "DELETE FROM metric_snapshots WHERE julianday(computed_at) < julianday('now', :mod)"),
    ("trust_score_snapshots",
     "DELETE FROM trust_score_snapshots WHERE julianday(computed_at) < julianday('now', :mod)"),
]


def _hours_modifier(days: float) -> str:
    """SQLite datetime modifier for `days` ago, expressed in whole hours so
    fractional-day windows (e.g. 0.5) are exact and unambiguous."""
    return f"-{int(round(days * 24))} hours"


def prune_once(audit_days: float, metrics_days: float) -> dict:
    """Run one retention sweep. Returns {table: rows_deleted}. SQLite only —
    the julianday()/freelist approach is SQLite-specific; on any other backend
    we skip (the platform ships on SQLite)."""
    if "sqlite" not in DATABASE_URL:
        return {"skipped": "non-sqlite backend"}

    counts: dict = {}
    with engine.begin() as conn:
        if audit_days and audit_days > 0:
            mod = _hours_modifier(audit_days)
            for table, sql in _AUDIT_DELETES:
                counts[table] = conn.execute(text(sql), {"mod": mod}).rowcount
        if metrics_days and metrics_days > 0:
            mod = _hours_modifier(metrics_days)
            for table, sql in _METRIC_DELETES:
                counts[table] = conn.execute(text(sql), {"mod": mod}).rowcount
    return counts


async def _retention_loop(audit_days: float, metrics_days: float, interval_min: int) -> None:
    # Let boot + seed settle before the first sweep so we don't contend with init.
    await asyncio.sleep(90)
    while True:
        try:
            counts = await asyncio.to_thread(prune_once, audit_days, metrics_days)
            if any(isinstance(v, int) and v for v in counts.values()):
                logger.info("[retention] swept %s (audit<=%sd, metrics<=%sd)",
                            counts, audit_days, metrics_days)
        except Exception as exc:  # never let a sweep kill the loop
            logger.warning("[retention] sweep failed: %s", exc)
        await asyncio.sleep(max(1, interval_min) * 60)


def start_retention() -> None:
    """Read env config and, if enabled, launch the background sweep task.
    Call from the FastAPI startup event (a running event loop is required)."""
    days = float(os.getenv("PAP_RETENTION_DAYS", "0") or 0)
    audit_days = float(os.getenv("PAP_RETENTION_AUDIT_DAYS", "") or days)
    metrics_days = float(os.getenv("PAP_RETENTION_METRICS_DAYS", "") or days)
    interval_min = int(os.getenv("PAP_RETENTION_INTERVAL_MIN", "60") or 60)

    if audit_days <= 0 and metrics_days <= 0:
        logger.info("[retention] disabled. Set PAP_RETENTION_DAYS=1 for a rolling "
                    "1-day window (or PAP_RETENTION_AUDIT_DAYS / _METRICS_DAYS to tune).")
        return

    logger.info("[retention] enabled — audit=%s, metrics=%s, every %d min",
                f"{audit_days}d" if audit_days > 0 else "off",
                f"{metrics_days}d" if metrics_days > 0 else "off", interval_min)
    asyncio.create_task(_retention_loop(audit_days, metrics_days, interval_min))
