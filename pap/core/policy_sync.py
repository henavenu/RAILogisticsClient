"""
Policy Sync — marks policies as published in the DB and triggers a hot-reload
on the Policy Engine.

The Policy Engine now reads policies directly from config_policies in the shared
PAP SQLite database.  No YAML file write is required on publish — the PE's
/internal/reload endpoint re-reads from DB.

The legacy policies.yaml write has been removed.  If you need a YAML export for
auditing or backup, use the PAP's /api/policies/export endpoint instead.
"""
import json
import logging
import httpx
from datetime import datetime
from pathlib import Path
from sqlalchemy.orm import Session
from core.models import Policy, PapAuditLog

logger = logging.getLogger(__name__)

import os
# Required (no silent fallback). routes.py validates POLICY_ENGINE_URL at import
# and aborts startup if unset, so by the time this runs it is guaranteed present.
POLICY_ENGINE_URL = os.getenv("POLICY_ENGINE_URL", "").strip()


def publish(db: Session, actor: str = "pap-admin", notes: str = "") -> dict:
    """
    1. Update published_at timestamps in DB
    2. Call POST /internal/reload on the Policy Engine
       (PE reads from config_policies WHERE active=1 — no YAML file needed)
    3. Write PUBLISH entries to audit_log

    Returns a status dict.
    """
    active_policies = db.query(Policy).filter(Policy.active == True).all()
    policy_count = len(active_policies)

    # ── Hot-reload ───────────────────────────────────────────────────────────
    # Trigger the PE to re-read policies from the DB.
    # The PE's /internal/reload now re-reads config_policies directly.
    reload_result  = {}
    reload_warning = None
    try:
        r = httpx.post(f"{POLICY_ENGINE_URL}/internal/reload", timeout=5.0)
        r.raise_for_status()
        reload_result = r.json()
        logger.info(f"Policy Engine hot-reload: {reload_result}")
    except Exception as e:
        reload_warning = (
            f"Policy Engine reload failed: {e}. "
            "Restart the Policy Engine to pick up changes."
        )
        logger.warning(reload_warning)

    # ── Update published_at + write audit entries ─────────────────────────
    now = datetime.utcnow()
    dimensions_published = []
    for p in active_policies:
        p.published_at = now
        dimensions_published.append(p.dimension)
        db.add(PapAuditLog(
            entity_type="policy",
            entity_id=p.dimension,
            app_id=p.app_id,
            action="PUBLISH",
            actor=actor,
            new_values=json.dumps({"version": p.version, "published_at": now.isoformat()}),
            notes=notes or None,
            timestamp=now,
        ))
    db.commit()

    result = {
        "success":      True,
        "policy_count": policy_count,
        "dimensions":   sorted(dimensions_published),
        "reload":       reload_result,
        "timestamp":    now.isoformat(),
    }
    if reload_warning:
        result["reload_warning"] = reload_warning

    return result
