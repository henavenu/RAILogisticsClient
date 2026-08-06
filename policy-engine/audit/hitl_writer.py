"""
HitlAlertWriter — inserts HITL platform alerts into the PAP's SQLite database.

Called by the PolicyOrchestrator when the ChainExecutor returns hitl=True
(i.e. a pack with hitl_trigger=true produced a non-PERMISSIVE verdict).

Uses plain sqlite3 (no SQLAlchemy / PAP ORM imports) and follows the same
thread-safety and error-isolation pattern as SqliteAuditWriter — all
exceptions are logged and swallowed so a failed alert write never degrades
the evaluation response.

Table written: platform_alerts
  category        = 'HITL'
  severity        = 'HIGH'
  app_id          = from request
  interaction_id  = from evaluation
  pack_id         = first HITL-triggering pack (others listed in payload)
  payload         = {verdict, hitl_packs, session_id, violations, source}
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

_INSERT_HITL_ALERT = """
INSERT INTO platform_alerts (
    category, severity,
    app_id, interaction_id, pack_id,
    payload,
    triggered_at
) VALUES (
    'HITL', 'HIGH',
    ?, ?, ?,
    ?,
    ?
)
"""


class HitlAlertWriter:
    """
    Thread-safe writer that inserts a HITL PlatformAlert into the PAP DB.

    One alert row per evaluation where chain_hitl=True.  Multiple packs
    can trigger HITL on a single evaluation; all are recorded in payload,
    and the pack_id column holds the first triggering pack.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = str(Path(db_path).resolve())
        self._lock    = threading.Lock()
        logger.info(f"[HitlAlertWriter] Initialised — DB: {self._db_path}")

    def emit(
        self,
        *,
        interaction_id: str,
        app_id:         Optional[str],
        session_id:     Optional[str],
        hitl_packs:     List[str],
        violations:     List[str],
        verdict:        str,
    ) -> None:
        """
        Insert one HITL alert row.  Never raises — all errors are logged.

        Parameters
        ----------
        interaction_id  Unique evaluation ID (links back to audit_evaluation_audit)
        app_id          Application that triggered the evaluation
        session_id      Active session (may be None for stateless requests)
        hitl_packs      Pack IDs that set hitl_trigger=true and produced a non-PERMISSIVE verdict
        violations      Human-readable violation strings from the evaluation response
        verdict         Final verdict string: NORMATIVE | COERCIVE
        """
        if not hitl_packs:
            return   # nothing to record

        if not Path(self._db_path).exists():
            logger.debug("[HitlAlertWriter] DB file not found — skipping emit")
            return

        primary_pack = hitl_packs[0]
        payload = json.dumps({
            "verdict":    verdict,
            "hitl_packs": hitl_packs,
            "session_id": session_id,
            "violations": violations,
            "source":     "policy_engine",
        })
        triggered_at = datetime.utcnow().isoformat(timespec="milliseconds")

        with self._lock:
            try:
                con = sqlite3.connect(self._db_path, timeout=10)
                con.execute(f"PRAGMA journal_mode={os.getenv('SQLITE_JOURNAL_MODE', 'WAL')}"); con.execute("PRAGMA busy_timeout=5000")
                try:
                    con.execute(_INSERT_HITL_ALERT, (
                        app_id,
                        interaction_id,
                        primary_pack,
                        payload,
                        triggered_at,
                    ))
                    con.commit()
                    logger.info(
                        f"[HitlAlertWriter] HITL alert emitted "
                        f"interaction={interaction_id[:8]} "
                        f"app={app_id} pack={primary_pack} verdict={verdict}"
                    )
                except Exception as exc:
                    con.rollback()
                    raise
                finally:
                    con.close()
            except Exception as exc:
                logger.error(
                    f"[HitlAlertWriter] Failed for interaction={interaction_id}: {exc}"
                )
