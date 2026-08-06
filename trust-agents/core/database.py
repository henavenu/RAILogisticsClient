"""
Trust Agent Store — SQLite schema and initialisation.

Three tables:
  ta_metric_snapshots — raw T1/T2 data collected each agent run
  ta_findings         — LLM-synthesised findings with reasoning trace
  ta_drift_signals    — active and resolved compliance/trust drift events

Plain sqlite3 — no SQLAlchemy, no Alembic.
Schema is created on startup via init_db(); idempotent (CREATE IF NOT EXISTS).
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# ── DDL ──────────────────────────────────────────────────────────────────────

_CREATE_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS ta_metric_snapshots (
    run_id          TEXT PRIMARY KEY,
    agent_type      TEXT NOT NULL,
    app_id          TEXT NOT NULL DEFAULT 'all',
    captured_at     TEXT NOT NULL,
    window_minutes  INTEGER NOT NULL,
    t1_json         TEXT,
    t2_json         TEXT,
    session_json    TEXT
)"""

_CREATE_FINDINGS = """
CREATE TABLE IF NOT EXISTS ta_findings (
    finding_id      TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    agent_type      TEXT NOT NULL,
    app_id          TEXT NOT NULL DEFAULT 'all',
    generated_at    TEXT NOT NULL,
    overall_status  TEXT NOT NULL,
    severity        TEXT NOT NULL,
    summary_text    TEXT,
    reasoning_trace TEXT,
    findings_json   TEXT,
    context_runs    INTEGER DEFAULT 0,
    FOREIGN KEY (run_id) REFERENCES ta_metric_snapshots(run_id)
)"""

_CREATE_DRIFT_SIGNALS = """
CREATE TABLE IF NOT EXISTS ta_drift_signals (
    signal_id        TEXT PRIMARY KEY,
    app_id           TEXT NOT NULL DEFAULT 'all',
    metric_path      TEXT NOT NULL,
    signal_type      TEXT NOT NULL,
    direction        TEXT NOT NULL,
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    resolved_at      TEXT,
    threshold_green  REAL,
    threshold_amber  REAL,
    current_value    REAL,
    consecutive_runs INTEGER DEFAULT 1
)"""

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_snap_agent_ts  ON ta_metric_snapshots(agent_type, captured_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_find_agent_ts  ON ta_findings(agent_type, generated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_drift_active   ON ta_drift_signals(metric_path, resolved_at)",
]


# ── Public API ────────────────────────────────────────────────────────────────

def init_db(db_path: str) -> None:
    """Create tables and indexes if they do not exist. Safe to call on every startup."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as con:
        con.execute(_CREATE_SNAPSHOTS)
        con.execute(_CREATE_FINDINGS)
        con.execute(_CREATE_DRIFT_SIGNALS)
        for idx in _INDEXES:
            con.execute(idx)
        con.commit()
    logger.info(f"[TrustAgentDB] Initialised — {db_path}")


def get_connection(db_path: str) -> sqlite3.Connection:
    """Return a WAL-mode connection with Row factory."""
    con = sqlite3.connect(db_path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con
