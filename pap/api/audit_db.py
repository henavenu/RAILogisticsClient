"""
Shared SQLite reader for evaluation audit records.

Replaces direct JSONL file reads in:
  /v1/audit/evaluations
  /v1/metrics/tier1
  /v1/metrics/apps-summary
  /v1/metrics/agent-activity

Returns records in the same dict shape as the JSONL audit trail so all
computation logic in each endpoint is unchanged.

Dict keys returned by load_audit_records():
  interaction_id, app_id, session_id, agent_id, role, verdict,
  session_verdict, session_escalated, timestamp (ISO str),
  latency_ms, input_tokens, output_tokens, model_id,
  metadata (dict — parsed from metadata_json column),
  fail_reason,
  dimension_results  (list of {dimension, score, score_normalized,
                                status, provider, is_violation})
"""
from __future__ import annotations

import json
import logging
import os

# Visualization-only app scope(s) — the platform Agent Activity lens writes
# agent_to_agent events under app_id="trust-platform" purely to draw the
# governance-plane graph. These must NOT appear in human-facing analytics
# (Eval Trail, metrics, cockpit). They are excluded from any UNSCOPED query
# (app_id=None); the topology graph still sees them by passing app_id explicitly.
_VIZ_APPS = [a.strip() for a in os.getenv("PAP_VIZ_APPS", "trust-platform").split(",") if a.strip()]
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def get_session_interactions(db: Session, session_id: str) -> list[dict]:
    """
    Return rows from audit_interaction_events for a session, sorted by
    seq_in_session (ascending).  Used by the dashboard interaction graph.

    Rows with a NULL seq_in_session (e.g. hitl_checkpoint events that the
    SDK writes without a session sequence) are pushed to the END rather than
    the front.  SQLite sorts NULL first under a plain "ASC", which previously
    made HITL checkpoints render as sequence 1; the CASE clause forces them
    last and "id ASC" preserves write order as the final tiebreaker.

    Falls back to an empty list if the table has not yet been created
    (i.e. the SqliteAuditWriter hasn't written any events yet).
    """
    sql = text("""
        SELECT id, session_id, app_id, turn_number,
               seq_in_session, seq_in_turn, timestamp,
               source_type, source_id, target_type, target_id,
               edge_type, verdict, violations,
               interaction_id, request_id
        FROM audit_interaction_events
        WHERE session_id = :session_id
        ORDER BY CASE WHEN seq_in_session IS NULL THEN 1 ELSE 0 END,
                 seq_in_session ASC,
                 id ASC
    """)
    try:
        rows = db.execute(sql, {"session_id": session_id}).fetchall()
    except Exception:
        logger.debug("[audit_db] audit_interaction_events not yet available for session %s", session_id)
        return []

    result: list[dict] = []
    for r in rows:
        violations = r.violations
        if isinstance(violations, str):
            try:
                violations = json.loads(violations)
            except Exception:
                violations = []
        elif not isinstance(violations, list):
            violations = []

        result.append({
            "id":             r.id,
            "session_id":     r.session_id,
            "app_id":         r.app_id,
            "turn_number":    r.turn_number,
            "seq_in_session": r.seq_in_session,
            "seq_in_turn":    r.seq_in_turn,
            "timestamp":      str(r.timestamp),
            "source_type":    r.source_type,
            "source_id":      r.source_id,
            "target_type":    r.target_type,
            "target_id":      r.target_id,
            "edge_type":      r.edge_type,
            "verdict":        r.verdict,
            "violations":     violations,
            "interaction_id": r.interaction_id,
            "request_id":     r.request_id,
        })
    return result


def load_audit_records(
    db: Session,
    *,
    limit: int = 500,
    since: Optional[datetime] = None,
    app_id: Optional[str] = None,
    verdict: Optional[str] = None,
) -> list[dict]:
    """
    Query audit_evaluation_audit joined with audit_evaluation_dimensions.

    Uses two queries + Python assembly (mirrors trust_score._read_records):
      1. Fetch up to `limit` audit rows matching the filters, newest-first.
      2. Fetch all dimension rows for those audit PKs in one IN-list query.
      3. Assemble dimension_results per audit row in Python.

    This avoids json_group_array / SQLite version dependencies and keeps
    the same "flat list of records" interface that the JSONL readers had.
    """
    filters: list[str] = []
    params: dict = {"limit": limit}

    if since is not None:
        filters.append("a.timestamp >= :since")
        params["since"] = since.replace(tzinfo=None).isoformat(timespec="seconds")

    if app_id:
        filters.append("a.app_id = :app_id")
        params["app_id"] = app_id
    elif _VIZ_APPS:
        # Unscoped query → hide the visualization-only governance-plane scope(s).
        for _i, _va in enumerate(_VIZ_APPS):
            filters.append(f"a.app_id != :viz_app_{_i}")
            params[f"viz_app_{_i}"] = _va

    if verdict:
        filters.append("a.verdict = :verdict")
        params["verdict"] = verdict.upper()

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    # ── Step 1: fetch audit rows ──────────────────────────────────────────────
    # Primary query includes migration-0011 columns (content_snippet, content_hash,
    # arize_trace_url).  Those columns only exist after migration 0011 has been
    # applied via `alembic upgrade head`.  If they are absent we fall back to the
    # original column set so the existing panels continue to work unchanged.
    # pack_trace is part of the original schema and is always available.
    _SQL_CORE = f"""
        SELECT a.id,
               a.interaction_id, a.app_id, a.session_id, a.agent_id,
               a.turn_number,
               a.role, a.verdict, a.session_verdict, a.session_escalated,
               a.timestamp, a.latency_ms,
               a.input_tokens, a.output_tokens, a.model_id,
               a.metadata_json, a.fail_reason, a.violations,
               a.pack_trace
        FROM audit_evaluation_audit a
        {where}
        ORDER BY a.timestamp DESC
        LIMIT :limit
    """
    _SQL_ENHANCED = f"""
        SELECT a.id,
               a.interaction_id, a.app_id, a.session_id, a.agent_id,
               a.turn_number,
               a.role, a.verdict, a.session_verdict, a.session_escalated,
               a.timestamp, a.latency_ms,
               a.input_tokens, a.output_tokens, a.model_id,
               a.metadata_json, a.fail_reason, a.violations,
               a.pack_trace,
               a.content_snippet, a.content_hash, a.arize_trace_url
        FROM audit_evaluation_audit a
        {where}
        ORDER BY a.timestamp DESC
        LIMIT :limit
    """

    audit_rows = None
    _has_content_cols = False
    try:
        audit_rows = db.execute(text(_SQL_ENHANCED), params).fetchall()
        _has_content_cols = True
    except Exception:
        # Columns from migration 0011 not yet present — fall back to core schema.
        logger.debug("[audit_db] content columns absent; falling back to core query")
        try:
            audit_rows = db.execute(text(_SQL_CORE), params).fetchall()
        except Exception:
            logger.exception("[audit_db] Error querying audit_evaluation_audit")
            return []

    if not audit_rows:
        return []

    # ── Step 2: fetch dimension rows for those PKs ────────────────────────────
    audit_ids = [r.id for r in audit_rows]
    id_csv    = ",".join(str(i) for i in audit_ids)

    dim_sql = text(f"""
        SELECT d.evaluation_audit_id,
               d.dimension_id, d.score, d.score_normalized,
               d.status, d.provider, d.is_violation
        FROM audit_evaluation_dimensions d
        WHERE d.evaluation_audit_id IN ({id_csv})
    """)

    try:
        dim_rows = db.execute(dim_sql).fetchall()
    except Exception:
        logger.exception("[audit_db] Error querying audit_evaluation_dimensions")
        dim_rows = []

    # ── Step 3: assemble dimension lookup ─────────────────────────────────────
    dims_by_id: dict[int, list[dict]] = {}
    for d in dim_rows:
        dims_by_id.setdefault(d.evaluation_audit_id, []).append({
            "dimension":        d.dimension_id,
            "score":            d.score,
            "score_normalized": d.score_normalized,
            "status":           d.status,
            "provider":         d.provider,
            "is_violation":     bool(d.is_violation),
        })

    # ── Step 4: build result list ─────────────────────────────────────────────
    result: list[dict] = []
    for a in audit_rows:
        meta = a.metadata_json or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}

        ts = a.timestamp
        if isinstance(ts, datetime):
            ts_str = ts.replace(tzinfo=timezone.utc).isoformat()
        else:
            ts_str = str(ts)

        # Parse violations JSON list written by SqliteAuditWriter
        raw_violations = a.violations
        if isinstance(raw_violations, str):
            try:
                parsed_violations = json.loads(raw_violations)
            except Exception:
                parsed_violations = []
        elif isinstance(raw_violations, list):
            parsed_violations = raw_violations
        else:
            parsed_violations = []

        # Parse pack_trace JSON (may be absent in older rows)
        raw_pack_trace = getattr(a, "pack_trace", None)
        if isinstance(raw_pack_trace, str):
            try:
                parsed_pack_trace = json.loads(raw_pack_trace)
            except Exception:
                parsed_pack_trace = []
        elif isinstance(raw_pack_trace, list):
            parsed_pack_trace = raw_pack_trace
        else:
            parsed_pack_trace = []

        result.append({
            "interaction_id":    a.interaction_id,
            "app_id":            a.app_id or "unknown",
            "session_id":        a.session_id,
            "agent_id":          a.agent_id,
            "turn_number":       a.turn_number,
            "role":              a.role,
            "verdict":           a.verdict,
            "session_verdict":   a.session_verdict,
            "session_escalated": bool(a.session_escalated),
            "timestamp":         ts_str,
            "latency_ms":        a.latency_ms,
            "input_tokens":      a.input_tokens,
            "output_tokens":     a.output_tokens,
            "model_id":          a.model_id,
            "metadata":          meta,
            "fail_reason":       a.fail_reason,
            "violations":        parsed_violations,
            "dimension_results": dims_by_id.get(a.id, []),
            # Content capture fields — None until migration 0011 runs
            "content_snippet":   (a.content_snippet   if _has_content_cols else None),
            "content_hash":      (a.content_hash      if _has_content_cols else None),
            "arize_trace_url":   (a.arize_trace_url   if _has_content_cols else None),
            # Pack trace for governance trace panel
            "pack_trace":        parsed_pack_trace,
            # function_name extracted from metadata for grouping
            "function_name":     meta.get("function_name") if isinstance(meta, dict) else None,
        })

    return result
