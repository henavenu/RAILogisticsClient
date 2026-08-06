"""
SqliteAuditWriter — persists every AuditRecord to the PAP's SQLite database.

Writes two rows per audit event (in one transaction):
  audit_evaluation_audit      — one row, INSERT OR IGNORE on interaction_id
  audit_evaluation_dimensions — one row per dimension_result entry

Uses Python's built-in sqlite3 — no SQLAlchemy dependency added to the PE.

Configuration
-------------
  AUDIT_SQLITE_PATH  absolute (or relative-to-PE-root) path to rai_trust.db.
                     If the variable is absent or the file does not yet exist
                     the writer silently no-ops so a cold-start (PE before PAP)
                     never crashes the engine.

Thread safety
-------------
  All writes are serialised through a threading.Lock.  Acceptable because
  evaluation throughput is already bounded by the Guardrail Service call,
  so the extra lock contention is negligible.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from audit.writer import AuditWriter, AuditRecord

# ── Content-capture config ────────────────────────────────────────────────────
# Set STORE_CONTENT_SNIPPETS=true to persist a truncated copy of each evaluated
# prompt/response.  Disabled by default — enable only when data retention policy
# permits storing prompt text (e.g. prototype / internal environments).
_STORE_SNIPPETS: bool = os.getenv("STORE_CONTENT_SNIPPETS", "").lower() in (
    "1", "true", "yes", "on"
)
# Maximum characters stored in content_snippet.  SHA-256 hash covers the full content.
_SNIPPET_MAX_CHARS: int = int(os.getenv("CONTENT_SNIPPET_MAX_CHARS", "500"))

logger = logging.getLogger(__name__)


# ── SQL templates ──────────────────────────────────────────────────────────────

_INSERT_AUDIT = """
INSERT OR IGNORE INTO audit_evaluation_audit (
    interaction_id, request_id, app_id, session_id, agent_id,
    turn_number, role, verdict, violations, remediations,
    fail_reason, session_verdict, session_escalated, latency_ms,
    enforcement_action, model_id, input_tokens, output_tokens,
    metadata_json, pack_trace, timestamp,
    content_snippet, content_hash
) VALUES (
    ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?,
    ?, ?, ?, ?,
    ?, ?, ?, ?,
    ?, ?, ?,
    ?, ?
)
"""

_SELECT_AUDIT_ID = "SELECT id FROM audit_evaluation_audit WHERE interaction_id = ?"

_INSERT_DIM = """
INSERT OR IGNORE INTO audit_evaluation_dimensions (
    evaluation_audit_id, dimension_id, score, score_normalized,
    is_violation, status, provider
) VALUES (?, ?, ?, ?, ?, ?, ?)
"""

# ── audit_interaction_events — explicit source/target edge table ───────────────
# One row per evaluation event with pre-computed source/target so the dashboard
# interaction graph never needs to infer actor relationships from metadata.
# Created on first write — safe to run CREATE IF NOT EXISTS repeatedly.

_CREATE_INTERACTION_TABLE = """
CREATE TABLE IF NOT EXISTS audit_interaction_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT    NOT NULL,
    app_id         TEXT,
    turn_number    INTEGER,
    seq_in_session INTEGER NOT NULL,
    seq_in_turn    INTEGER NOT NULL,
    timestamp      TEXT    NOT NULL,
    source_type    TEXT    NOT NULL,
    source_id      TEXT    NOT NULL,
    target_type    TEXT    NOT NULL,
    target_id      TEXT    NOT NULL,
    edge_type      TEXT    NOT NULL,
    verdict        TEXT,
    violations     TEXT,
    interaction_id TEXT    UNIQUE,
    request_id     TEXT
)
"""

_CREATE_INTERACTION_IDX_SESSION = """
CREATE INDEX IF NOT EXISTS idx_aie_session
    ON audit_interaction_events (session_id)
"""

_CREATE_INTERACTION_IDX_SEQ = """
CREATE INDEX IF NOT EXISTS idx_aie_session_seq
    ON audit_interaction_events (session_id, seq_in_session)
"""

_INSERT_INTERACTION = """
INSERT OR IGNORE INTO audit_interaction_events (
    session_id, app_id, turn_number,
    seq_in_session, seq_in_turn,
    timestamp, source_type, source_id, target_type, target_id,
    edge_type, verdict, violations, interaction_id, request_id
) VALUES (
    ?, ?, ?,
    ?, ?,
    ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?
)
"""


# ── A2A source-agent fallback table ──────────────────────────────────────────
# Maps tool_name → source_agent for tools where the SDK cannot determine the
# calling node at on_tool_start time (on_chat_model_start didn't fire because
# the calling node makes synthetic tool calls without going through the LLM).
# Extend as new tools are added to a2a_tools in the SDK instrument() call.
_A2A_SOURCE_BY_TOOL: dict = {
    "request_card_research": "card_advisor",   # card_advisor_node (synthetic rounds)
}


class SqliteAuditWriter(AuditWriter):
    """
    Thread-safe writer that persists AuditRecord to the PAP's SQLite database.

    INSERT OR IGNORE on interaction_id means duplicate events from retries are
    silently dropped.  Dimension rows use a composite UNIQUE on
    (evaluation_audit_id, dimension_id) for the same reason.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = str(Path(db_path).resolve())
        self._lock    = threading.Lock()
        # Sequence counters for audit_interaction_events.
        # Incremented inside self._lock so they're thread-safe.
        self._session_seq: dict = {}   # session_id → monotonic counter within session
        self._turn_seq:    dict = {}   # (session_id, turn_number) → counter within turn
        self._interaction_table_ok: bool = False  # True after first CREATE IF NOT EXISTS
        logger.info(f"[SqliteAuditWriter] Initialised — DB: {self._db_path}")

    # ── Public interface ───────────────────────────────────────────────────────

    def get_session_max_turn(self, session_id: str) -> "int | None":
        """
        Return the highest turn_number recorded in audit_interaction_events for
        session_id, or None if no rows exist.

        Used by the orchestrator to restore turn_count when a session state is
        recreated after TTL expiry or PE restart — prevents turn numbers from
        resetting to 1/2 within the same session.  Thread-safe.
        """
        try:
            with self._lock:
                with __import__("sqlite3").connect(self._db_path) as _con:
                    row = _con.execute(
                        "SELECT MAX(turn_number) FROM audit_interaction_events "
                        "WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()
                    return int(row[0]) if row and row[0] is not None else None
        except Exception as exc:
            logger.debug("[SqliteAuditWriter] get_session_max_turn failed: %s", exc)
            return None

    def write(self, record: AuditRecord) -> None:
        """Persist record + its dimension scores to SQLite.  Never raises."""
        if not Path(self._db_path).exists():
            # PAP not yet started — skip silently (will catch next write)
            return

        audit_params = self._build_audit_params(record)

        with self._lock:
            try:
                con = sqlite3.connect(self._db_path, timeout=10)
                con.execute(f"PRAGMA journal_mode={os.getenv('SQLITE_JOURNAL_MODE', 'WAL')}"); con.execute("PRAGMA busy_timeout=5000")
                con.execute("PRAGMA foreign_keys=ON")
                try:
                    cur = con.execute(_INSERT_AUDIT, audit_params)
                    if cur.rowcount == 0:
                        # interaction_id already present — skip (idempotent)
                        return

                    audit_id = cur.lastrowid
                    self._write_dimensions(con, audit_id, record.dimension_results or [])
                    self._write_interaction_event(con, record)
                    con.commit()

                    logger.debug(
                        f"[SqliteAuditWriter] Written: interaction_id={record.interaction_id} "
                        f"role={record.role} verdict={record.verdict} "
                        f"dims={len(record.dimension_results or [])}"
                    )
                except Exception as exc:
                    con.rollback()
                    raise
                finally:
                    con.close()
            except Exception as exc:
                logger.error(
                    f"[SqliteAuditWriter] Failed for {record.interaction_id}: {exc}"
                )

    # ── Private helpers ────────────────────────────────────────────────────────

    def _build_audit_params(self, record: AuditRecord) -> tuple:
        """Map AuditRecord fields → positional params for _INSERT_AUDIT."""
        # Extract token data written by the SDK middleware into metadata
        at: dict = {}
        if isinstance(record.metadata, dict):
            at = record.metadata.get("agent_tokens") or {}
        model_id      = at.get("model")      or None
        input_tokens  = at.get("prompt_tokens")    or None
        output_tokens = at.get("completion_tokens") or None

        # Normalize timestamp to naive ISO string for SQLite DateTime column
        ts_str = record.timestamp
        try:
            ts     = datetime.fromisoformat(ts_str)
            ts_str = ts.replace(tzinfo=None).isoformat(timespec="milliseconds")
        except (ValueError, AttributeError, TypeError):
            pass  # keep original string — SQLite will still store it

        # ── Content capture (opt-in via STORE_CONTENT_SNIPPETS env var) ──────────
        content_snippet: Optional[str] = None
        content_hash:    Optional[str] = None
        if _STORE_SNIPPETS and record.content:
            raw = record.content
            content_snippet = raw[:_SNIPPET_MAX_CHARS]
            content_hash    = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()

        return (
            record.interaction_id,
            record.request_id,
            record.app_id,
            record.session_id,
            record.agent_id,
            record.turn_number,
            record.role,
            record.verdict,
            json.dumps(record.violations   or []),
            json.dumps(record.remediations or []),
            record.fail_reason,
            record.session_verdict,
            int(record.session_escalated),
            record.latency_ms,
            record.enforcement_action,
            model_id,
            input_tokens,
            output_tokens,
            json.dumps(record.metadata)    if record.metadata   else None,
            json.dumps(record.pack_trace   or []),
            ts_str,
            content_snippet,
            content_hash,
        )

    @staticmethod
    def _write_dimensions(con: sqlite3.Connection, audit_id: int, dim_results: list) -> None:
        """Insert one row per dimension result for the given audit record."""
        for dr in dim_results:
            raw_score  = dr.get("score")
            score_int  = int(raw_score) if raw_score is not None else None
            score_norm = round((score_int - 1) / 8.0, 4) if score_int is not None else None
            status     = dr.get("status", "PASS")
            con.execute(_INSERT_DIM, (
                audit_id,
                dr.get("dimension", ""),
                score_int,
                score_norm,
                1 if status in ("WARNING", "FLAGGED") else 0,
                status,
                dr.get("provider"),
            ))

    @staticmethod
    def _derive_edge(record: AuditRecord) -> Optional[tuple]:
        """
        Derive the interaction edge for an audit record.

        Returns (source_type, source_id, target_type, target_id) or None
        when no meaningful edge can be derived (e.g. same-agent A2A with
        no distinguishing tool_name, or unknown role).

        Edge type taxonomy
        ------------------
        user_prompt      : user → agent
        agent_to_agent   : agent → agent  (or → tool when source == target)
        tool_call        : agent → tool
        llm_response     : agent → user
        hitl_checkpoint  : platform → hitl
        """
        role = record.role
        meta = record.metadata or {}

        # "agent_name" is the canonical field; "agent" is the legacy key emitted
        # by the PAP's _write_hitl_audit_to_pe before the naming convention was
        # standardised.  Accept either so old in-flight PAP events are attributed.
        agent_name   = meta.get("agent_name") or meta.get("agent") or record.agent_id or "agent"
        tool_name    = meta.get("tool_name")    or ""
        source_agent = meta.get("source_agent") or ""
        target_agent = meta.get("target_agent") or ""

        if role == "user_prompt":
            return ("user", "user", "agent", agent_name)

        if role == "agent_to_agent":
            # _A2A_SOURCE_BY_TOOL takes highest priority: the SDK can incorrectly
            # propagate the previous target as source_agent for rounds 2+ of a
            # multi-round A2A loop (e.g. card_advisor calls request_card_research
            # 5 times; after round 1 SDK sets _current_node="card_researcher" so
            # source_agent becomes "card_researcher" in rounds 2-5 — wrong).
            # When the tool_name identifies the A2A call, we always use the
            # hardcoded caller, regardless of what source_agent says.
            fixed_src = _A2A_SOURCE_BY_TOOL.get(tool_name, "")
            src = fixed_src or source_agent or agent_name
            tgt = target_agent or agent_name
            if src == tgt:
                # Neither source nor target distinguished — emit a platform
                # sentinel edge so the target agent still appears in the graph.
                # POST-MVP: ensure source_agent+target_agent are always supplied.
                return ("platform", "platform", "agent", tgt)
            return ("agent", src, "agent", tgt)

        if role == "tool_call":
            # Skip tool_call events for known A2A tools: the agent_to_agent event
            # already represents this interaction with correct source attribution.
            # The tool_call would carry the wrong agent_name (the subagent target
            # rather than the calling agent) and would add noise to the graph.
            if tool_name in _A2A_SOURCE_BY_TOOL:
                return None
            return ("agent", agent_name, "tool", tool_name or "tool")

        if role == "llm_response":
            return ("agent", agent_name, "user", "user")

        if role == "hitl_checkpoint":
            src = agent_name or record.app_id or "platform"
            return ("platform", src, "hitl", "hitl")

        return None  # Unknown role — skip

    def _write_interaction_event(
        self,
        con: sqlite3.Connection,
        record: AuditRecord,
    ) -> None:
        """
        Insert one row into audit_interaction_events for this record.

        Called inside the same SQLite transaction as _INSERT_AUDIT so
        the two rows are always atomic.  Creates the table on first use
        (CREATE TABLE IF NOT EXISTS is idempotent and fast).

        Sequence counters (_session_seq, _turn_seq) are incremented here;
        they are thread-safe because all writes are serialised via self._lock.
        """
        # Create table + indexes on first write (once per process lifetime)
        if not self._interaction_table_ok:
            con.execute(_CREATE_INTERACTION_TABLE)
            con.execute(_CREATE_INTERACTION_IDX_SESSION)
            con.execute(_CREATE_INTERACTION_IDX_SEQ)
            self._interaction_table_ok = True

        edge = self._derive_edge(record)
        if edge is None:
            return  # No meaningful edge — skip silently

        source_type, source_id, target_type, target_id = edge

        # Advance per-session and per-turn monotonic sequence counters.
        # Restore from DB on first use after a PE restart so seq numbers are
        # always monotonically increasing across process restarts.
        sid  = record.session_id or ""
        tnum = record.turn_number

        if sid not in self._session_seq:
            try:
                row = con.execute(
                    "SELECT COALESCE(MAX(seq_in_session), 0) FROM audit_interaction_events WHERE session_id=?",
                    (sid,),
                ).fetchone()
                self._session_seq[sid] = int(row[0]) if row else 0
            except Exception:
                self._session_seq[sid] = 0

        self._session_seq[sid] += 1
        seq_in_session = self._session_seq[sid]

        tk = (sid, tnum)
        if tk not in self._turn_seq:
            try:
                row = con.execute(
                    "SELECT COALESCE(MAX(seq_in_turn), 0) FROM audit_interaction_events "
                    "WHERE session_id=? AND turn_number=?",
                    (sid, tnum),
                ).fetchone()
                self._turn_seq[tk] = int(row[0]) if row else 0
            except Exception:
                self._turn_seq[tk] = 0

        self._turn_seq[tk] += 1
        seq_in_turn = self._turn_seq[tk]

        con.execute(_INSERT_INTERACTION, (
            sid,
            record.app_id,
            tnum,
            seq_in_session,
            seq_in_turn,
            record.timestamp,
            source_type,
            source_id,
            target_type,
            target_id,
            record.role,        # edge_type = the content role
            record.verdict,
            json.dumps(record.violations or []),
            record.interaction_id,
            record.request_id,
        ))
