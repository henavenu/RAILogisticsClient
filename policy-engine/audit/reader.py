"""
AuditReader — read-side access to the PAP's SQLite audit tables.

Used by the Sentinel pull endpoint (GET /v1/audit/events) so the Sentinel
Agent can fetch the last-N evaluation events for a session and compute
cross-turn aggregates (structuring, salami, session-level patterns) that
the per-turn PE evaluation cannot see.

Queries two tables (same ones SqliteAuditWriter writes to):
  audit_evaluation_audit      — one row per evaluation
  audit_evaluation_dimensions — one row per dimension per evaluation

Thread safety
─────────────
Uses a per-call sqlite3.connect() with WAL mode (same pattern as
SqliteAuditWriter and HitlAlertWriter) — safe under concurrent read/write.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# ── SQL ───────────────────────────────────────────────────────────────────────

_SELECT_EVENTS = """
SELECT
    a.interaction_id,
    a.session_id,
    a.app_id,
    a.turn_number,
    a.role,
    a.verdict,
    a.violations,
    a.remediations,
    a.timestamp,
    a.latency_ms,
    a.session_verdict,
    a.session_escalated,
    a.enforcement_action,
    a.metadata_json,
    a.content_snippet
FROM audit_evaluation_audit a
{where_clause}
ORDER BY a.turn_number DESC, a.timestamp DESC
LIMIT ?
"""

_SELECT_DIMS_FOR_EVENTS = """
SELECT
    a.interaction_id,
    d.dimension_id,
    d.score,
    d.score_normalized,
    d.is_violation,
    d.status,
    d.provider
FROM audit_evaluation_dimensions d
JOIN audit_evaluation_audit a ON a.id = d.evaluation_audit_id
{where_clause}
ORDER BY a.turn_number DESC
"""


class AuditReader:
    """
    Read-only access to the PAP's SQLite audit tables.

    Parameters
    ----------
    db_path : str
        Absolute path to the PAP's rai_trust.db file (same value as
        ``AUDIT_SQLITE_PATH`` used by SqliteAuditWriter).
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = str(Path(db_path).resolve())
        logger.info(f"[AuditReader] Initialised — DB: {self._db_path}")

    # ── Public ─────────────────────────────────────────────────────────────────

    def get_events(
        self,
        *,
        session_id:     Optional[str] = None,
        app_id:         Optional[str] = None,
        role:           Optional[str] = None,
        limit:          int           = 20,
        include_dims:   bool          = True,
    ) -> List[dict]:
        """
        Fetch recent audit events, newest first.

        At least one of ``session_id`` or ``app_id`` must be provided.

        Parameters
        ----------
        session_id
            Filter to a specific session (most common Sentinel use case).
        app_id
            Secondary filter — can be combined with ``session_id`` or used alone.
        role
            Optional content-role filter (``"user_prompt"``, ``"tool_call"``, …).
        limit
            Maximum events to return (capped at 200 server-side).
        include_dims
            When True (default), attach per-dimension scores to each event.

        Returns
        -------
        list[dict]
            Events sorted newest-first.  Each dict has the keys:
            ``interaction_id``, ``session_id``, ``app_id``, ``turn_number``,
            ``role``, ``verdict``, ``violations``, ``remediations``,
            ``timestamp``, ``latency_ms``, ``session_verdict``,
            ``session_escalated``, ``enforcement_action``, ``metadata``,
            ``content_snippet`` (non-null only when STORE_CONTENT_SNIPPETS=true
            in the Policy Engine), and optionally ``dimension_results``.
        """
        if not session_id and not app_id:
            raise ValueError("At least one of session_id or app_id must be provided")

        limit = min(int(limit), 200)

        if not Path(self._db_path).exists():
            logger.debug("[AuditReader] DB file not found — returning empty list")
            return []

        try:
            # ── Build WHERE clause ─────────────────────────────────────────
            conditions: list[str] = []
            params:     list      = []

            if session_id:
                conditions.append("a.session_id = ?")
                params.append(session_id)
            if app_id:
                conditions.append("a.app_id = ?")
                params.append(app_id)
            if role:
                conditions.append("a.role = ?")
                params.append(role)

            where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

            con = sqlite3.connect(self._db_path, timeout=10)
            con.row_factory = sqlite3.Row
            con.execute(f"PRAGMA journal_mode={os.getenv('SQLITE_JOURNAL_MODE', 'WAL')}")
            con.execute("PRAGMA busy_timeout=5000")
            con.execute("PRAGMA query_only=ON")

            try:
                rows = con.execute(
                    _SELECT_EVENTS.format(where_clause=where_clause),
                    params + [limit],
                ).fetchall()

                events = [self._row_to_dict(r) for r in rows]

                if include_dims and events:
                    self._attach_dimensions(con, events, session_id=session_id, app_id=app_id, role=role)

            finally:
                con.close()

            logger.debug(
                f"[AuditReader] get_events → {len(events)} events "
                f"session={session_id or '-'}  app={app_id or '-'}"
            )
            return events

        except Exception as exc:
            logger.error(f"[AuditReader] get_events failed: {exc}")
            return []

    # ── Private helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        """Convert a sqlite3.Row from audit_evaluation_audit to a plain dict."""
        return {
            "interaction_id":   row["interaction_id"],
            "session_id":       row["session_id"],
            "app_id":           row["app_id"],
            "turn_number":      row["turn_number"],
            "role":             row["role"],
            "verdict":          row["verdict"],
            "violations":       _json_load(row["violations"],   default=[]),
            "remediations":     _json_load(row["remediations"], default=[]),
            "timestamp":        row["timestamp"],
            "latency_ms":       row["latency_ms"],
            "session_verdict":  row["session_verdict"],
            "session_escalated": bool(row["session_escalated"]),
            "enforcement_action": row["enforcement_action"],
            "metadata":         _json_load(row["metadata_json"], default=None),
            # content_snippet is non-null only when STORE_CONTENT_SNIPPETS=true
            # in the Policy Engine .env.  Sentinel's LLM judge uses this to
            # reason about actual prompt/response text, not just metadata.
            "content_snippet":  row["content_snippet"] if row["content_snippet"] else None,
            "dimension_results": [],   # filled in by _attach_dimensions
        }

    def _attach_dimensions(
        self,
        con:        sqlite3.Connection,
        events:     List[dict],
        *,
        session_id: Optional[str],
        app_id:     Optional[str],
        role:       Optional[str],
    ) -> None:
        """
        Fetch dimension rows for the returned events and attach them in-place.

        Uses a single JOIN query scoped to the same WHERE conditions + LIMIT to
        avoid fetching dimensions for rows we don't need.
        """
        # Index events by interaction_id for O(1) lookup
        event_map = {e["interaction_id"]: e for e in events}
        iid_list  = list(event_map.keys())
        if not iid_list:
            return

        # Build an IN clause parameterised on the exact interaction IDs we have
        placeholders = ", ".join("?" * len(iid_list))
        dim_query = f"""
            SELECT
                a.interaction_id,
                d.dimension_id,
                d.score,
                d.score_normalized,
                d.is_violation,
                d.status,
                d.provider
            FROM audit_evaluation_dimensions d
            JOIN audit_evaluation_audit a ON a.id = d.evaluation_audit_id
            WHERE a.interaction_id IN ({placeholders})
        """
        try:
            dim_rows = con.execute(dim_query, iid_list).fetchall()
            for dr in dim_rows:
                iid = dr["interaction_id"]
                if iid in event_map:
                    event_map[iid]["dimension_results"].append({
                        "dimension":        dr["dimension_id"],
                        "score":            dr["score"],
                        "score_normalized": dr["score_normalized"],
                        "is_violation":     bool(dr["is_violation"]),
                        "status":           dr["status"],
                        "provider":         dr["provider"],
                    })
        except Exception as exc:
            logger.warning(f"[AuditReader] _attach_dimensions failed (non-fatal): {exc}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _json_load(value, *, default):
    """Parse a JSON string stored in SQLite, returning ``default`` on failure."""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default
