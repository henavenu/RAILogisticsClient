"""
VerdictStore — SQLite-backed persistent store for SentinelVerdict objects.

Keyed by session_id.  Each new analysis upserts the latest verdict so the
dashboard always sees the current picture for a session.

Design
──────
• SQLite (stdlib sqlite3) — no extra dependency, file survives service restarts.
• DB path: SENTINEL_DB_PATH env var (default: ./sentinel.db next to main.py).
• Thread-safety: threading.Lock() guards every operation.
• Schema migration: ALTER TABLE adds new behavioral columns on first startup
  after an upgrade — safe for existing databases.
• all() returns newest evaluated_at first (not insertion order).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from typing import List, Optional

from schemas import FiredCheck, SentinelVerdict

logger = logging.getLogger(__name__)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS sentinel_verdicts (
    session_id        TEXT PRIMARY KEY,
    app_id            TEXT,
    verdict           TEXT NOT NULL DEFAULT 'CLEAR',
    trigger_reason    TEXT DEFAULT 'N_TURN',
    fired_checks      TEXT DEFAULT '[]',
    llm_used          INTEGER DEFAULT 0,
    llm_reasoning     TEXT DEFAULT '',
    evaluated_at      TEXT,
    interaction_id    TEXT DEFAULT '',
    event_count       INTEGER DEFAULT 0,
    pull_failed       INTEGER DEFAULT 0,
    -- Behavioral classification columns (added in v2 — migrated on startup)
    pattern_type      TEXT,
    rai_pillar        TEXT,
    rai_dimensions    TEXT DEFAULT '[]',
    agents_implicated TEXT DEFAULT '[]',
    confidence        REAL,
    evidence_turns    TEXT DEFAULT '[]'
)
"""

# Columns added in the v2 schema that may not exist in older databases.
_MIGRATION_COLUMNS = [
    ("pattern_type",      "TEXT"),
    ("rai_pillar",        "TEXT"),
    ("rai_dimensions",    "TEXT DEFAULT '[]'"),
    ("agents_implicated", "TEXT DEFAULT '[]'"),
    ("confidence",        "REAL"),
    ("evidence_turns",    "TEXT DEFAULT '[]'"),
]


def _row_to_verdict(row: sqlite3.Row) -> SentinelVerdict:
    fired = json.loads(row["fired_checks"] or "[]")
    keys  = row.keys()
    return SentinelVerdict(
        session_id        = row["session_id"],
        app_id            = row["app_id"],
        verdict           = row["verdict"],
        trigger_reason    = row["trigger_reason"] or "N_TURN",
        fired_checks      = [FiredCheck(**c) for c in fired],
        llm_used          = bool(row["llm_used"]),
        llm_reasoning     = row["llm_reasoning"] or "",
        evaluated_at      = row["evaluated_at"] or "",
        interaction_id    = row["interaction_id"] or "",
        event_count       = row["event_count"] or 0,
        pull_failed       = bool(row["pull_failed"]),
        # Behavioral classification — gracefully absent in older rows
        pattern_type      = row["pattern_type"]      if "pattern_type"      in keys else None,
        rai_pillar        = row["rai_pillar"]         if "rai_pillar"        in keys else None,
        rai_dimensions    = json.loads(row["rai_dimensions"]    or "[]") if "rai_dimensions"    in keys else [],
        agents_implicated = json.loads(row["agents_implicated"] or "[]") if "agents_implicated" in keys else [],
        confidence        = row["confidence"]         if "confidence"        in keys else None,
        evidence_turns    = json.loads(row["evidence_turns"]    or "[]") if "evidence_turns"    in keys else [],
    )


class VerdictStore:
    """SQLite-backed store: session_id → latest SentinelVerdict."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or os.getenv("SENTINEL_DB_PATH", "./sentinel.db")
        self._lock    = threading.Lock()
        self._init_db()
        logger.info(f"[VerdictStore] SQLite store initialised at {self._db_path}")

    # ── Private helpers ───────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create table and apply v2 column migrations (idempotent)."""
        with self._connect() as conn:
            conn.execute(_CREATE_SQL)
            conn.commit()
            # Migrate: add new behavioral columns to existing databases
            for col, col_def in _MIGRATION_COLUMNS:
                try:
                    conn.execute(
                        f"ALTER TABLE sentinel_verdicts ADD COLUMN {col} {col_def}"
                    )
                    conn.commit()
                    logger.info(f"[VerdictStore] Migrated: added column '{col}'")
                except sqlite3.OperationalError:
                    pass  # column already exists — nothing to do

    # ── Public interface ──────────────────────────────────────────────────────

    # Verdict severity ranking — higher number = more severe.
    _VERDICT_RANK: dict = {"CLEAR": 0, "WATCH": 1, "ALERT": 2}

    def put(self, verdict: SentinelVerdict, authoritative: bool = False) -> None:
        """Upsert the latest verdict for a session.

        Severity is a one-way ratchet — it can only increase, never decrease.
        This applies to BOTH heuristic (Phase A) and LLM (Phase B) writes,
        preventing non-deterministic LLM output from clearing a breach that
        the deterministic pre-checks already established.

        When the LLM would downgrade (e.g. it returns WATCH but heuristics
        already wrote ALERT), the VERDICT level is preserved at its higher
        value but ALL LLM metadata (pattern, pillar, reasoning, confidence,
        dimensions, evidence_turns) is still written — so the record is
        enriched with the LLM's classification without losing the alert floor.

        Parameters
        ----------
        verdict       The SentinelVerdict to store.
        authoritative When True (Phase B — LLM), metadata is always written
                      and the verdict is upgraded if the LLM rates it higher.
                      When False (Phase A — heuristic), a lower verdict is
                      silently skipped entirely to prevent race-condition
                      overwrites from concurrent N_TURN webhooks.
        """
        fired_json = json.dumps([c.model_dump() for c in verdict.fired_checks])

        with self._lock:
            with self._connect() as conn:
                # Read current stored verdict (if any) to enforce the ratchet.
                row = conn.execute(
                    "SELECT verdict FROM sentinel_verdicts WHERE session_id = ?",
                    (verdict.session_id,),
                ).fetchone()

                current_rank = self._VERDICT_RANK.get(row["verdict"], 0) if row else -1
                new_rank     = self._VERDICT_RANK.get(verdict.verdict, 0)
                would_downgrade = (row is not None) and (new_rank < current_rank)

                if would_downgrade and not authoritative:
                    # Phase A heuristic trying to downgrade: skip entirely.
                    logger.info(
                        f"[VerdictStore] Skipping heuristic downgrade "
                        f"{verdict.verdict} < {row['verdict']} "
                        f"for session={verdict.session_id}"
                    )
                    return

                # Determine the verdict level to actually persist.
                # Ratchet: preserve the higher of current vs new.
                stored_verdict = row["verdict"] if would_downgrade else verdict.verdict

                if would_downgrade:
                    # Phase B (LLM) wants to downgrade — keep higher verdict
                    # but enrich the record with LLM metadata.
                    logger.info(
                        f"[VerdictStore] LLM would downgrade "
                        f"{verdict.verdict} < {row['verdict']} — "
                        f"preserving {row['verdict']}, writing LLM metadata only "
                        f"for session={verdict.session_id}"
                    )
                    conn.execute("""
                        UPDATE sentinel_verdicts SET
                            llm_used          = ?,
                            llm_reasoning     = ?,
                            evaluated_at      = ?,
                            pattern_type      = ?,
                            rai_pillar        = ?,
                            rai_dimensions    = ?,
                            agents_implicated = ?,
                            confidence        = ?,
                            evidence_turns    = ?
                        WHERE session_id = ?
                    """, (
                        int(verdict.llm_used),
                        verdict.llm_reasoning,
                        verdict.evaluated_at,
                        verdict.pattern_type,
                        verdict.rai_pillar,
                        json.dumps(verdict.rai_dimensions),
                        json.dumps(verdict.agents_implicated),
                        verdict.confidence,
                        json.dumps(verdict.evidence_turns),
                        verdict.session_id,
                    ))
                else:
                    conn.execute("""
                        INSERT INTO sentinel_verdicts (
                            session_id, app_id, verdict, trigger_reason, fired_checks,
                            llm_used, llm_reasoning, evaluated_at, interaction_id,
                            event_count, pull_failed,
                            pattern_type, rai_pillar, rai_dimensions,
                            agents_implicated, confidence, evidence_turns
                        )
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(session_id) DO UPDATE SET
                            app_id            = excluded.app_id,
                            verdict           = excluded.verdict,
                            trigger_reason    = excluded.trigger_reason,
                            fired_checks      = excluded.fired_checks,
                            llm_used          = excluded.llm_used,
                            llm_reasoning     = excluded.llm_reasoning,
                            evaluated_at      = excluded.evaluated_at,
                            interaction_id    = excluded.interaction_id,
                            event_count       = excluded.event_count,
                            pull_failed       = excluded.pull_failed,
                            pattern_type      = excluded.pattern_type,
                            rai_pillar        = excluded.rai_pillar,
                            rai_dimensions    = excluded.rai_dimensions,
                            agents_implicated = excluded.agents_implicated,
                            confidence        = excluded.confidence,
                            evidence_turns    = excluded.evidence_turns
                    """, (
                        verdict.session_id,
                        verdict.app_id,
                        stored_verdict,
                        verdict.trigger_reason,
                        fired_json,
                        int(verdict.llm_used),
                        verdict.llm_reasoning,
                        verdict.evaluated_at,
                        verdict.interaction_id,
                        verdict.event_count,
                        int(verdict.pull_failed),
                        verdict.pattern_type,
                        verdict.rai_pillar,
                        json.dumps(verdict.rai_dimensions),
                        json.dumps(verdict.agents_implicated),
                        verdict.confidence,
                        json.dumps(verdict.evidence_turns),
                    ))
                conn.commit()
        logger.info(
            f"[VerdictStore] Stored verdict={stored_verdict} "
            f"(requested={verdict.verdict}) "
            f"session={verdict.session_id} "
            f"pattern={verdict.pattern_type or '-'} "
            f"pillar={verdict.rai_pillar or '-'} "
            f"llm_used={verdict.llm_used} "
            f"authoritative={authoritative}"
        )

    def get(self, session_id: str) -> Optional[SentinelVerdict]:
        """Return the latest verdict for a session, or None if not yet analysed."""
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM sentinel_verdicts WHERE session_id = ?",
                    (session_id,)
                ).fetchone()
        return _row_to_verdict(row) if row else None

    def delete(self, session_id: str) -> None:
        """Remove a session's verdict so it no longer appears on the radar.

        Used to suppress council deliberation sessions (HITL-only) — and to
        clear any finding a pre-council webhook may have already written.
        """
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM sentinel_verdicts WHERE session_id = ?",
                    (session_id,),
                )
                conn.commit()

    def all(self) -> List[SentinelVerdict]:
        """Return all stored verdicts, newest evaluated_at first."""
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM sentinel_verdicts ORDER BY evaluated_at DESC"
                ).fetchall()
        return [_row_to_verdict(r) for r in rows]

    def __len__(self) -> int:
        with self._lock:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT COUNT(*) FROM sentinel_verdicts"
                ).fetchone()[0]
