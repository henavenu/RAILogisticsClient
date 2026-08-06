"""
GovernanceAlertsWriter — inserts POLICY_VIOLATION governance alerts into the
PAP's governance_alerts table when a zero-tolerance dimension fires a COERCIVE
verdict.

Design
------
Two-source model:
  Primary source   : Sentinel duo raises INCIDENT / ADVISORY from cross-turn
                     patterns and writes them via the PAP REST API.
  Safety-net source: This writer — PE raises POLICY_VIOLATION immediately on
                     the first COERCIVE verdict for any zero-tolerance dimension
                     without waiting for the Sentinel window.

Zero-tolerance dimensions are loaded from the PAP DB at startup (the
zero_tolerance=1 flag set by migration 0015).  A local in-memory cache avoids
a DB hit on every evaluation.

Deduplication
-------------
One POLICY_VIOLATION row per (session_id, dimension).  If a matching open row
already exists, its recurrence_count is incremented and last_seen_at updated.
New rows are inserted when no matching open row exists (or when session_id is
NULL — stateless evaluations always get a fresh row).

Thread safety
-------------
All writes are serialised through a per-instance threading.Lock.  Plain
sqlite3 is used (no SQLAlchemy / PAP ORM imports) so this module can run
inside the Policy Engine process without importing PAP dependencies.

Error isolation
---------------
All exceptions are caught, logged, and swallowed — a failed governance alert
write NEVER degrades the evaluation response returned to the SDK caller.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# ── SQL statements ─────────────────────────────────────────────────────────────

_SELECT_ZERO_TOLERANCE = """
SELECT dimension_id
FROM   config_evaluation_dimensions
WHERE  zero_tolerance = 1
"""

# Check for an existing open POLICY_VIOLATION for this (session, dimension) pair.
# Used for deduplication — if found we increment rather than insert.
_SELECT_EXISTING = """
SELECT id, recurrence_count
FROM   governance_alerts
WHERE  tier       = 'POLICY_VIOLATION'
AND    status     = 'open'
AND    dimension  = ?
AND    session_id = ?
LIMIT  1
"""

_UPDATE_RECURRENCE = """
UPDATE governance_alerts
SET    recurrence_count = recurrence_count + 1,
       last_seen_at     = ?,
       updated_at       = ?
WHERE  id = ?
"""

_INSERT_ALERT = """
INSERT INTO governance_alerts (
    tier, severity,
    raiser, raiser_detail,
    caller_type, caller_id,
    app_id,
    reason_code, reason_text, condition,
    dimension, pack_id,
    interaction_id, session_id,
    recurrence_count,
    payload,
    status,
    triggered_at,
    created_at, updated_at
) VALUES (
    'POLICY_VIOLATION', 'URGENT',
    'policy_engine', ?,
    'session', ?,
    ?,
    'coercive_verdict', ?, ?,
    ?, ?,
    ?, ?,
    1,
    ?,
    'open',
    ?,
    ?, ?
)
"""


class GovernanceAlertsWriter:
    """
    Thread-safe writer that emits POLICY_VIOLATION governance alerts for
    zero-tolerance dimension breaches detected by the Policy Engine.

    Instantiate once at PE startup; call emit() after each evaluation where
    the final verdict is COERCIVE.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = str(Path(db_path).resolve())
        self._lock    = threading.Lock()
        self._zero_tolerance_dims: frozenset[str] = frozenset()
        self._load_zero_tolerance_dims()

    # ── Initialisation ─────────────────────────────────────────────────────────

    def _load_zero_tolerance_dims(self) -> None:
        """Load zero-tolerance dimension IDs from the PAP DB at startup."""
        if not Path(self._db_path).exists():
            logger.warning(
                "[GovernanceAlertsWriter] DB not found at startup — "
                "zero-tolerance dimension set is empty; emit() will no-op"
            )
            return
        try:
            con = sqlite3.connect(self._db_path, timeout=5)
            try:
                rows = con.execute(_SELECT_ZERO_TOLERANCE).fetchall()
                self._zero_tolerance_dims = frozenset(r[0] for r in rows)
                logger.info(
                    f"[GovernanceAlertsWriter] Loaded {len(self._zero_tolerance_dims)} "
                    f"zero-tolerance dimensions: {sorted(self._zero_tolerance_dims)}"
                )
            finally:
                con.close()
        except Exception as exc:
            logger.error(
                f"[GovernanceAlertsWriter] Failed to load zero-tolerance dimensions: {exc}"
            )

    # ── Public API ─────────────────────────────────────────────────────────────

    def should_alert(self, dimension: str, dim_verdict: str) -> bool:
        """
        Return True when a POLICY_VIOLATION alert should be emitted.

        Criteria:
          • The dimension is in the zero-tolerance set
          • The per-dimension signal is COERCIVE (PE verdict) OR FLAGGED (GS raw
            status — score 7-9 from the Guardrail Service, which maps to COERCIVE
            for zero-tolerance dimensions under normal policy thresholds)

        Note: the orchestrator now passes the PE's per-dimension policy verdict
        (COERCIVE/NORMATIVE/PERMISSIVE) computed by policy_registry.evaluate(),
        not the Guardrail Service raw status (PASS/WARNING/FLAGGED).
        """
        return (
            dim_verdict.upper() == "COERCIVE"
            and dimension in self._zero_tolerance_dims
        )

    def emit(
        self,
        *,
        interaction_id: str,
        app_id:         Optional[str],
        session_id:     Optional[str],
        role:           str,
        dimension:      str,
        dim_score:      Optional[int],
        pack_id:        Optional[str],
        violations:     List[str],
        final_verdict:  str,
    ) -> None:
        """
        Emit one POLICY_VIOLATION governance alert.

        Deduplication: if an open POLICY_VIOLATION for (session_id, dimension)
        already exists, increment recurrence_count instead of inserting a new row.
        Stateless requests (session_id=None) always get a fresh row.

        Parameters
        ----------
        interaction_id  Unique evaluation ID
        app_id          Application identifier (may be None)
        session_id      Session context (may be None for stateless requests)
        role            Evaluation role: user_prompt | llm_response | …
        dimension       The zero-tolerance dimension that fired
        dim_score       Raw dimension score (1-9) from the Guardrail Service
        pack_id         Policy pack that enforced this dimension (may be None)
        violations      Human-readable violation strings from the evaluation
        final_verdict   Final synthesised verdict (COERCIVE | NORMATIVE | …)
        """
        if not Path(self._db_path).exists():
            logger.debug("[GovernanceAlertsWriter] DB not found — skipping emit")
            return

        now_iso = datetime.utcnow().isoformat(timespec="milliseconds")

        # Build human-readable fields
        score_str  = f"score={dim_score}" if dim_score is not None else "score=?"
        condition  = f"{dimension} {score_str} — COERCIVE verdict (zero-tolerance breach)"
        reason_txt = (
            f"Zero-tolerance dimension '{dimension}' produced a COERCIVE verdict "
            f"during {role} evaluation for app '{app_id or 'unknown'}'."
        )
        raiser_detail = f"pe_eval:{role}:{dimension}"
        caller_id     = session_id or interaction_id

        payload_json = json.dumps({
            "final_verdict": final_verdict,
            "dimension":     dimension,
            "dim_score":     dim_score,
            "pack_id":       pack_id,
            "role":          role,
            "violations":    violations,
            "source":        "policy_engine:zero_tolerance",
        })

        with self._lock:
            try:
                con = sqlite3.connect(self._db_path, timeout=10)
                con.execute(f"PRAGMA journal_mode={os.getenv('SQLITE_JOURNAL_MODE', 'WAL')}"); con.execute("PRAGMA busy_timeout=5000")
                try:
                    # ── Deduplication check ──────────────────────────────────
                    existing_id   = None
                    existing_recur = None
                    if session_id:
                        row = con.execute(_SELECT_EXISTING, (dimension, session_id)).fetchone()
                        if row:
                            existing_id, existing_recur = row

                    if existing_id is not None:
                        # Increment recurrence on the existing open alert
                        con.execute(_UPDATE_RECURRENCE, (now_iso, now_iso, existing_id))
                        con.commit()
                        logger.info(
                            f"[GovernanceAlertsWriter] POLICY_VIOLATION recurrence "
                            f"id={existing_id} count={existing_recur + 1} "
                            f"dimension={dimension} session={session_id} app={app_id}"
                        )
                    else:
                        # Insert a new alert row
                        con.execute(_INSERT_ALERT, (
                            raiser_detail,   # raiser_detail
                            caller_id,       # caller_id
                            app_id,          # app_id
                            reason_txt,      # reason_text
                            condition,       # condition
                            dimension,       # dimension
                            pack_id,         # pack_id
                            interaction_id,  # interaction_id
                            session_id,      # session_id
                            payload_json,    # payload
                            now_iso,         # triggered_at
                            now_iso,         # created_at
                            now_iso,         # updated_at
                        ))
                        con.commit()
                        logger.info(
                            f"[GovernanceAlertsWriter] POLICY_VIOLATION alert emitted "
                            f"dimension={dimension} interaction={interaction_id[:8]} "
                            f"app={app_id} session={session_id}"
                        )
                except Exception as exc:
                    con.rollback()
                    raise
                finally:
                    con.close()

            except Exception as exc:
                logger.error(
                    f"[GovernanceAlertsWriter] Failed for "
                    f"interaction={interaction_id} dimension={dimension}: {exc}"
                )
