"""
Governance Alerts API.

Unified, tiered alert store for the RAI Trust Platform.

REST endpoints (external — operator/compliance dashboard)
─────────────────────────────────────────────────────────
  GET   /v1/governance/alerts              — list with filtering
  GET   /v1/governance/alerts/{id}         — single alert detail
  PATCH /v1/governance/alerts/{id}/status  — lifecycle transitions
  GET   /v1/governance/alerts/summary      — tier/severity counts (dashboard KPIs)

Internal endpoints (PE, Sentinel, SRE — localhost / internal network)
──────────────────────────────────────────────────────────────────────
  POST  /internal/governance/alerts        — create an alert (PE / Sentinel writer)
  PATCH /internal/governance/alerts/{id}/enrich  — Coordinator async enrichment

SSE streaming
─────────────
  GET   /internal/governance/alerts/stream — real-time push to dashboard clients
      Query params:
        last_id (int, default 0)           — resume from this alert id
        tiers   (csv, optional)            — e.g. "INCIDENT,POLICY_VIOLATION"

SSE event types
────────────────
  gov_alert_new       — new alert created
  gov_alert_updated   — status or enrichment changed
  heartbeat           — keepalive every 15 s
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from core.database import get_db, SessionLocal
from core.models   import GovernanceAlert

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Constants ──────────────────────────────────────────────────────────────────

_VALID_TIERS = {"INCIDENT", "HITL", "POLICY_VIOLATION", "RELIABILITY", "ADVISORY"}
_VALID_STATUSES = {
    "open", "acknowledged", "in_review", "resolved", "suppressed", "false_positive"
}
_VALID_RESOLUTIONS = {
    "acknowledged", "override", "escalated", "auto_resolved", "false_positive"
}

_SSE_POLL_INTERVAL  = 2.5   # seconds
_SSE_HEARTBEAT_TICK = 15    # seconds

# ── SSE helpers ────────────────────────────────────────────────────────────────

def _sse_encode(event: str, data: dict, alert_id: Optional[int] = None) -> str:
    lines: list[str] = []
    if alert_id is not None:
        lines.append(f"id: {alert_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data)}")
    return "\n".join(lines) + "\n\n"


async def _gov_alert_stream(last_id: int, tiers: Optional[str]):
    tier_filter: Optional[list[str]] = (
        [t.strip().upper() for t in tiers.split(",") if t.strip()]
        if tiers else None
    )
    last_new_id     = last_id
    last_updated_ts = datetime.utcnow()
    ticks           = 0

    while True:
        await asyncio.sleep(_SSE_POLL_INTERVAL)
        ticks += 1

        if ticks * _SSE_POLL_INTERVAL >= _SSE_HEARTBEAT_TICK:
            yield _sse_encode("heartbeat", {"ts": datetime.utcnow().isoformat()})
            ticks = 0

        try:
            with SessionLocal() as db:
                # New alerts
                q = (
                    db.query(GovernanceAlert)
                    .filter(GovernanceAlert.id > last_new_id)
                    .order_by(GovernanceAlert.id.asc())
                    .limit(50)
                )
                if tier_filter:
                    q = q.filter(GovernanceAlert.tier.in_(tier_filter))
                new_alerts = q.all()

                # Updated alerts (recurrence increments, enrichment, status changes)
                updated = (
                    db.query(GovernanceAlert)
                    .filter(
                        GovernanceAlert.id <= last_new_id,
                        GovernanceAlert.updated_at.isnot(None),
                        GovernanceAlert.updated_at > last_updated_ts,
                    )
                    .order_by(GovernanceAlert.updated_at.asc())
                    .limit(50)
                    .all()
                )

        except Exception as exc:
            logger.warning(f"[Gov SSE] DB poll failed: {exc}")
            yield _sse_encode("heartbeat", {"error": str(exc), "ts": datetime.utcnow().isoformat()})
            continue

        for alert in new_alerts:
            yield _sse_encode("gov_alert_new", alert.to_dict(), alert_id=alert.id)
            last_new_id = max(last_new_id, alert.id)

        for alert in updated:
            yield _sse_encode("gov_alert_updated", alert.to_dict(), alert_id=alert.id)
            if alert.updated_at:
                last_updated_ts = max(last_updated_ts, alert.updated_at)


# ── SSE endpoint ───────────────────────────────────────────────────────────────

@router.get(
    "/internal/governance/alerts/stream",
    summary="Real-time governance alert stream (SSE)",
    include_in_schema=False,
)
async def stream_governance_alerts(
    last_id: int           = Query(0,    description="Resume from this alert id"),
    tiers:   Optional[str] = Query(None, description="CSV tier filter, e.g. INCIDENT,HITL"),
):
    return StreamingResponse(
        _gov_alert_stream(last_id=last_id, tiers=tiers),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── REST — create (internal) ───────────────────────────────────────────────────

class CreateAlertBody(BaseModel):
    tier:          str
    severity:      str             = "HIGH"
    raiser:        str
    raiser_detail: Optional[str]   = None
    caller_type:   Optional[str]   = None
    caller_id:     Optional[str]   = None
    app_id:        Optional[str]   = None
    reason_code:   Optional[str]   = None
    reason_text:   Optional[str]   = None
    condition:     Optional[str]   = None
    dimension:     Optional[str]   = None
    pack_id:       Optional[str]   = None
    interaction_id: Optional[str]  = None
    session_id:    Optional[str]   = None
    evidence_turns: Optional[List] = None
    payload:       Optional[dict]  = None
    # Coordinator fields — optionally supplied at creation time so a single
    # POST captures the full recommendation without a follow-up PATCH/enrich.
    coord_action:  Optional[str]   = None
    coord_target:  Optional[str]   = None
    coord_itsm:    Optional[dict]  = None


@router.post(
    "/internal/governance/alerts",
    status_code=201,
    summary="Create a governance alert (internal — PE / Sentinel)",
    include_in_schema=False,
)
def create_governance_alert(
    body: CreateAlertBody,
    db:   Session = Depends(get_db),
):
    tier = body.tier.upper()
    if tier not in _VALID_TIERS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid tier '{tier}'. Must be one of: {sorted(_VALID_TIERS)}",
        )

    alert = GovernanceAlert(
        tier           = tier,
        severity       = body.severity.upper(),
        raiser         = body.raiser,
        raiser_detail  = body.raiser_detail,
        caller_type    = body.caller_type,
        caller_id      = body.caller_id,
        app_id         = body.app_id,
        reason_code    = body.reason_code,
        reason_text    = body.reason_text,
        condition      = body.condition,
        dimension      = body.dimension,
        pack_id        = body.pack_id,
        interaction_id = body.interaction_id,
        session_id     = body.session_id,
        evidence_turns = body.evidence_turns,
        payload        = body.payload,
        coord_action   = body.coord_action,
        coord_target   = body.coord_target,
        coord_itsm     = body.coord_itsm,
        status         = "open",
        triggered_at   = datetime.utcnow(),
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    logger.info(
        f"[gov_alerts] Alert created: id={alert.id} tier={tier} "
        f"raiser={body.raiser} app={body.app_id} "
        f"coord_action={body.coord_action or '-'}"
    )
    return alert.to_dict()


# ── REST — Coordinator enrichment (internal) ───────────────────────────────────

class EnrichBody(BaseModel):
    coord_action: Optional[str]  = None
    coord_target: Optional[str]  = None
    coord_itsm:   Optional[dict] = None


@router.patch(
    "/internal/governance/alerts/{alert_id}/enrich",
    summary="Coordinator async enrichment — add action/target/itsm (internal)",
    include_in_schema=False,
)
def enrich_governance_alert(
    alert_id: int,
    body: EnrichBody,
    db:   Session = Depends(get_db),
):
    alert = db.query(GovernanceAlert).filter(GovernanceAlert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")

    if body.coord_action is not None:
        alert.coord_action = body.coord_action
    if body.coord_target is not None:
        alert.coord_target = body.coord_target
    if body.coord_itsm is not None:
        alert.coord_itsm = body.coord_itsm
    alert.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(alert)
    logger.info(
        f"[gov_alerts] Alert {alert_id} enriched by Coordinator — "
        f"action={body.coord_action} target={body.coord_target}"
    )
    return alert.to_dict()


# ── REST — list ────────────────────────────────────────────────────────────────

@router.get(
    "/v1/governance/alerts",
    summary="List governance alerts",
)
def list_governance_alerts(
    tier:       Optional[str]  = Query(None, description="Filter by tier (INCIDENT, HITL, …)"),
    severity:   Optional[str]  = Query(None, description="Filter by severity (URGENT, HIGH, …)"),
    status:     Optional[str]  = Query(None, description="Filter by status (open, resolved, …)"),
    app_id:     Optional[str]  = Query(None, description="Filter by application ID"),
    session_id: Optional[str]  = Query(None, description="Filter by session ID"),
    dimension:  Optional[str]  = Query(None, description="Filter by RAI dimension"),
    raiser:     Optional[str]  = Query(None, description="Filter by raiser (sentinel, policy_engine, …)"),
    limit:      int             = Query(100, ge=1, le=1000),
    offset:     int             = Query(0,   ge=0),
    db:         Session         = Depends(get_db),
):
    q = db.query(GovernanceAlert)
    if tier:
        q = q.filter(GovernanceAlert.tier == tier.upper())
    if severity:
        q = q.filter(GovernanceAlert.severity == severity.upper())
    if status:
        q = q.filter(GovernanceAlert.status == status.lower())
    if app_id:
        q = q.filter(GovernanceAlert.app_id == app_id)
    if session_id:
        q = q.filter(GovernanceAlert.session_id == session_id)
    if dimension:
        q = q.filter(GovernanceAlert.dimension == dimension)
    if raiser:
        q = q.filter(GovernanceAlert.raiser == raiser.lower())

    total  = q.count()
    alerts = q.order_by(GovernanceAlert.triggered_at.desc()).offset(offset).limit(limit).all()
    return {
        "total":  total,
        "offset": offset,
        "limit":  limit,
        "alerts": [a.to_dict() for a in alerts],
    }


# ── REST — summary (dashboard KPIs) ───────────────────────────────────────────

@router.get(
    "/v1/governance/alerts/summary",
    summary="Governance alert counts by tier and severity",
)
def governance_alerts_summary(
    app_id: Optional[str] = Query(None),
    db:     Session        = Depends(get_db),
):
    """
    Returns open alert counts grouped by tier and severity — suitable for
    the dashboard KPI strip and the new Governance Alerts tab header.
    """
    q = db.query(
        GovernanceAlert.tier,
        GovernanceAlert.severity,
        func.count(GovernanceAlert.id).label("count"),
    ).filter(GovernanceAlert.status == "open")

    if app_id:
        q = q.filter(GovernanceAlert.app_id == app_id)

    rows = q.group_by(GovernanceAlert.tier, GovernanceAlert.severity).all()

    # Aggregate totals per tier
    by_tier: dict = {}
    for tier, severity, count in rows:
        if tier not in by_tier:
            by_tier[tier] = {"total": 0, "by_severity": {}}
        by_tier[tier]["total"]                 += count
        by_tier[tier]["by_severity"][severity]  = count

    grand_total = sum(v["total"] for v in by_tier.values())
    return {
        "total_open": grand_total,
        "by_tier":    by_tier,
    }


# ── REST — single ──────────────────────────────────────────────────────────────

@router.get(
    "/v1/governance/alerts/{alert_id}",
    summary="Get a single governance alert",
)
def get_governance_alert(
    alert_id: int,
    db: Session = Depends(get_db),
):
    alert = db.query(GovernanceAlert).filter(GovernanceAlert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")
    return alert.to_dict()


# ── REST — status transitions ──────────────────────────────────────────────────

class StatusPatchBody(BaseModel):
    status:          str
    resolution:      Optional[str] = None
    resolution_note: Optional[str] = None
    resolved_by:     Optional[str] = None
    acknowledged_by: Optional[str] = None


@router.patch(
    "/v1/governance/alerts/{alert_id}/status",
    summary="Update governance alert lifecycle status",
)
def patch_governance_alert_status(
    alert_id: int,
    body: StatusPatchBody,
    db:   Session = Depends(get_db),
):
    alert = db.query(GovernanceAlert).filter(GovernanceAlert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")

    new_status = body.status.lower()
    if new_status not in _VALID_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid status '{new_status}'. Must be one of: {sorted(_VALID_STATUSES)}",
        )

    now = datetime.utcnow()
    alert.status     = new_status
    alert.updated_at = now

    if new_status == "acknowledged":
        alert.acknowledged_at = now
        alert.acknowledged_by = body.acknowledged_by or "operator"

    elif new_status in ("resolved", "suppressed", "false_positive"):
        alert.resolved_at   = now
        alert.resolved_by   = body.resolved_by or "operator"
        if body.resolution:
            if body.resolution not in _VALID_RESOLUTIONS:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid resolution '{body.resolution}'. "
                           f"Must be one of: {sorted(_VALID_RESOLUTIONS)}",
                )
            alert.resolution = body.resolution
        alert.resolution_note = body.resolution_note

    db.commit()
    db.refresh(alert)
    logger.info(
        f"[gov_alerts] Alert {alert_id} → {new_status} "
        f"by {body.resolved_by or body.acknowledged_by or 'operator'}"
    )
    return alert.to_dict()
