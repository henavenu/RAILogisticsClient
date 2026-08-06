"""
ReportStore — SQLite-backed persistent store for CoordinatorReport objects.

Keyed by session_id.  Each new coordination run upserts the latest report.

Design
──────
• SQLite (stdlib sqlite3) — no extra dependency, file survives service restarts.
• DB path: COORDINATOR_DB_PATH env var (default: ./coordinator.db next to main.py).
• Thread-safety: threading.Lock() guards every operation.
• Schema migration: ALTER TABLE adds new columns on first startup after upgrade.
• all() returns newest coordinated_at first.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional

from schemas import CoordinatorReport, Intervention

logger = logging.getLogger(__name__)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS coordinator_reports (
    session_id          TEXT PRIMARY KEY,
    app_id              TEXT,
    sentinel_verdict    TEXT DEFAULT 'CLEAR',
    overall_risk        TEXT DEFAULT 'LOW',
    interventions       TEXT DEFAULT '[]',
    summary             TEXT DEFAULT '',
    llm_used            INTEGER DEFAULT 0,
    coordinated_at      TEXT,
    source_interaction  TEXT DEFAULT '',
    -- Behavioral context columns (v2)
    pattern_type        TEXT,
    rai_pillar          TEXT
)
"""

_MIGRATION_COLUMNS = [
    ("interventions",  "TEXT DEFAULT '[]'"),
    ("pattern_type",   "TEXT"),
    ("rai_pillar",     "TEXT"),
]


def _row_to_report(row: sqlite3.Row) -> CoordinatorReport:
    keys = row.keys()

    # Prefer new interventions column; fall back to empty list
    interventions_raw = json.loads(row["interventions"] or "[]") if "interventions" in keys else []
    interventions = []
    for item in interventions_raw:
        try:
            interventions.append(Intervention(**item))
        except Exception:
            pass  # skip malformed items from older schema

    return CoordinatorReport(
        session_id         = row["session_id"],
        app_id             = row["app_id"],
        sentinel_verdict   = row["sentinel_verdict"] or "CLEAR",
        overall_risk       = row["overall_risk"] or "LOW",
        pattern_type       = row["pattern_type"]  if "pattern_type" in keys else None,
        rai_pillar         = row["rai_pillar"]    if "rai_pillar"   in keys else None,
        interventions      = interventions,
        summary            = row["summary"] or "",
        llm_used           = bool(row["llm_used"]),
        coordinated_at     = row["coordinated_at"] or "",
        source_interaction = row["source_interaction"] or "",
    )


class ReportStore:
    """SQLite-backed store: session_id → latest CoordinatorReport."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or os.getenv("COORDINATOR_DB_PATH", "./coordinator.db")
        self._lock    = threading.Lock()
        self._init_db()
        logger.info(f"[ReportStore] SQLite store initialised at {self._db_path}")

    # ── Private helpers ───────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create table and apply migrations (idempotent)."""
        with self._connect() as conn:
            conn.execute(_CREATE_SQL)
            conn.commit()
            for col, col_def in _MIGRATION_COLUMNS:
                try:
                    conn.execute(
                        f"ALTER TABLE coordinator_reports ADD COLUMN {col} {col_def}"
                    )
                    conn.commit()
                    logger.info(f"[ReportStore] Migrated: added column '{col}'")
                except sqlite3.OperationalError:
                    pass  # already exists

    # ── Public interface ──────────────────────────────────────────────────────

    def put(self, report: CoordinatorReport) -> None:
        """Upsert the latest report for a session."""
        interventions_json = json.dumps(
            [i.model_dump() for i in report.interventions]
        )
        with self._lock:
            with self._connect() as conn:
                conn.execute("""
                    INSERT INTO coordinator_reports (
                        session_id, app_id, sentinel_verdict, overall_risk,
                        interventions, summary, llm_used, coordinated_at,
                        source_interaction, pattern_type, rai_pillar
                    )
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        app_id             = excluded.app_id,
                        sentinel_verdict   = excluded.sentinel_verdict,
                        overall_risk       = excluded.overall_risk,
                        interventions      = excluded.interventions,
                        summary            = excluded.summary,
                        llm_used           = excluded.llm_used,
                        coordinated_at     = excluded.coordinated_at,
                        source_interaction = excluded.source_interaction,
                        pattern_type       = excluded.pattern_type,
                        rai_pillar         = excluded.rai_pillar
                """, (
                    report.session_id,
                    report.app_id,
                    report.sentinel_verdict,
                    report.overall_risk,
                    interventions_json,
                    report.summary,
                    int(report.llm_used),
                    report.coordinated_at,
                    report.source_interaction,
                    report.pattern_type,
                    report.rai_pillar,
                ))
                conn.commit()
        logger.info(
            f"[ReportStore] Stored report session={report.session_id} "
            f"risk={report.overall_risk} "
            f"pattern={report.pattern_type or '-'} "
            f"interventions={len(report.interventions)}"
        )

    def get(self, session_id: str) -> Optional[CoordinatorReport]:
        """Return the latest report for a session, or None if not yet coordinated."""
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM coordinator_reports WHERE session_id = ?",
                    (session_id,)
                ).fetchone()
        return _row_to_report(row) if row else None

    def all(self) -> List[CoordinatorReport]:
        """Return all stored reports, newest coordinated_at first."""
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM coordinator_reports ORDER BY coordinated_at DESC"
                ).fetchall()
        return [_row_to_report(r) for r in rows]

    def __len__(self) -> int:
        with self._lock:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT COUNT(*) FROM coordinator_reports"
                ).fetchone()[0]
