"""
Platform Alerts API.

REST endpoints:
  GET  /v1/alerts                  — list alerts with filtering
  GET  /v1/alerts/{id}             — single alert
  PUT  /v1/alerts/{id}/resolve     — resolve an open alert
  POST /v1/alerts/test             — emit a test alert (dev/test only)

SSE streaming:
  GET  /internal/alerts/stream     — real-time push to dashboard clients
      Query params:
        last_id   (int, default 0)  — resume from this alert id; client
                                       stores the last received id and
                                       sends it on reconnect
        categories (csv, optional)  — comma-separated filter, e.g.
                                       "HITL,PROVIDER_UNAVAILABLE"

SSE event types (event: field):
  alert_new          — a new alert was inserted (any category)
  provider_alert     — shorthand for PROVIDER_UNAVAILABLE category alerts
  hitl_queued        — shorthand for HITL category alerts
  alert_resolved     — an existing open alert was resolved
  heartbeat          — keepalive tick (sent every 15 s, no data payload)

Event data payload is the alert's to_dict() JSON.

Auth note: /internal/... routes are intended for localhost / internal
network use only.  Add a shared-secret middleware if exposed externally.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.database import get_db, SessionLocal
from core.models   import PlatformAlert

logger = logging.getLogger(__name__)

router = APIRouter()

# ── SSE helpers ────────────────────────────────────────────────────────────────

_CATEGORY_TO_EVENT: dict[str, str] = {
    "PROVIDER_UNAVAILABLE": "provider_alert",
    "HITL":                 "hitl_queued",
    "THRESHOLD_BREACH":     "alert_new",
    "EVAL_ANOMALY":         "alert_new",
}

_SSE_POLL_INTERVAL  = 2.5   # seconds between DB polls
_SSE_HEARTBEAT_TICK = 15    # seconds between keepalive ticks (must be > poll interval)


def _sse_encode(event: str, data: dict, alert_id: Optional[int] = None) -> str:
    """Format one SSE frame."""
    lines: list[str] = []
    if alert_id is not None:
        lines.append(f"id: {alert_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data)}")
    return "\n".join(lines) + "\n\n"


async def _alert_stream_generator(
    last_id: int,
    categories: Optional[str],
):
    """
    Async generator that polls platform_alerts and yields SSE frames.

    Tracks two watermarks:
      last_new_id      — highest id already sent as alert_new/provider_alert/hitl_queued
      last_resolved_ts — ISO timestamp; polls resolved_at > this for resolved events

    These are held in memory; the client-side last_id query param handles
    reconnect catch-up for new alerts.
    """
    cat_filter: Optional[list[str]] = (
        [c.strip().upper() for c in categories.split(",") if c.strip()]
        if categories
        else None
    )

    last_new_id       = last_id
    last_resolved_ts  = datetime.utcnow()
    ticks_since_hb    = 0

    while True:
        await asyncio.sleep(_SSE_POLL_INTERVAL)
        ticks_since_hb += 1

        # ── Heartbeat ─────────────────────────────────────────────────────────
        if ticks_since_hb * _SSE_POLL_INTERVAL >= _SSE_HEARTBEAT_TICK:
            yield _sse_encode("heartbeat", {"ts": datetime.utcnow().isoformat()})
            ticks_since_hb = 0

        # ── Poll DB for new alerts ─────────────────────────────────────────────
        try:
            with SessionLocal() as db:
                q = (
                    db.query(PlatformAlert)
                    .filter(PlatformAlert.id > last_new_id)
                    .order_by(PlatformAlert.id.asc())
                    .limit(50)
                )
                if cat_filter:
                    q = q.filter(PlatformAlert.category.in_(cat_filter))
                new_alerts = q.all()

                # ── Poll for newly resolved alerts ────────────────────────────
                resolved = (
                    db.query(PlatformAlert)
                    .filter(
                        PlatformAlert.resolved_at.isnot(None),
                        PlatformAlert.resolved_at > last_resolved_ts,
                    )
                    .order_by(PlatformAlert.resolved_at.asc())
                    .limit(50)
                    .all()
                )

        except Exception as exc:
            logger.warning(f"[SSE] DB poll failed: {exc}")
            yield _sse_encode("heartbeat", {"error": str(exc), "ts": datetime.utcnow().isoformat()})
            continue

        # Emit new alert events
        for alert in new_alerts:
            event_type = _CATEGORY_TO_EVENT.get(alert.category, "alert_new")
            yield _sse_encode(event_type, alert.to_dict(), alert_id=alert.id)
            last_new_id = max(last_new_id, alert.id)

        # Emit resolution events
        for alert in resolved:
            yield _sse_encode("alert_resolved", alert.to_dict(), alert_id=alert.id)
            last_resolved_ts = max(last_resolved_ts, alert.resolved_at)


# ── SSE endpoint ───────────────────────────────────────────────────────────────

@router.get(
    "/internal/alerts/stream",
    summary="Real-time alert stream (SSE)",
    description=(
        "Server-Sent Events stream.  Connect with EventSource in the browser. "
        "Sends `alert_new`, `provider_alert`, `hitl_queued`, `alert_resolved`, "
        "and `heartbeat` events."
    ),
)
async def stream_alerts(
    last_id:    int            = Query(0,    description="Resume from this alert id (client reconnect)"),
    categories: Optional[str] = Query(None, description="CSV category filter, e.g. HITL,PROVIDER_UNAVAILABLE"),
):
    return StreamingResponse(
        _alert_stream_generator(last_id=last_id, categories=categories),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


# ── REST — list alerts ─────────────────────────────────────────────────────────

@router.get(
    "/v1/alerts",
    summary="List platform alerts",
)
def list_alerts(
    category:  Optional[str]  = Query(None, description="Filter by category"),
    resolved:  Optional[bool] = Query(None, description="True=resolved only, False=open only"),
    app_id:    Optional[str]  = Query(None),
    limit:     int             = Query(100,  ge=1, le=1000),
    offset:    int             = Query(0,    ge=0),
    db:        Session         = Depends(get_db),
):
    q = db.query(PlatformAlert)
    if category:
        q = q.filter(PlatformAlert.category == category.upper())
    if resolved is True:
        q = q.filter(PlatformAlert.resolved_at.isnot(None))
    elif resolved is False:
        q = q.filter(PlatformAlert.resolved_at.is_(None))
    if app_id:
        q = q.filter(PlatformAlert.app_id == app_id)

    total  = q.count()
    alerts = q.order_by(PlatformAlert.triggered_at.desc()).offset(offset).limit(limit).all()
    return {
        "total":  total,
        "offset": offset,
        "limit":  limit,
        "alerts": [a.to_dict() for a in alerts],
    }


# ── REST — single alert ────────────────────────────────────────────────────────

@router.get(
    "/v1/alerts/{alert_id}",
    summary="Get a single alert",
)
def get_alert(
    alert_id: int,
    db: Session = Depends(get_db),
):
    alert = db.query(PlatformAlert).filter(PlatformAlert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")
    return alert.to_dict()


# ── REST — resolve alert ───────────────────────────────────────────────────────

class ResolveBody(BaseModel):
    resolution:      str           = "acknowledged"
    resolution_note: Optional[str] = None
    resolved_by:     Optional[str] = None


@router.put(
    "/v1/alerts/{alert_id}/resolve",
    summary="Resolve an open alert",
)
def resolve_alert(
    alert_id: int,
    body: ResolveBody,
    db:   Session = Depends(get_db),
):
    alert = db.query(PlatformAlert).filter(PlatformAlert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")
    if alert.resolved_at is not None:
        return {
            "status":  "already_resolved",
            "alert_id": alert_id,
            "resolved_at": alert.resolved_at.isoformat(),
        }

    alert.resolved_at    = datetime.utcnow()
    alert.resolved_by    = body.resolved_by or "pap-admin"
    alert.resolution     = body.resolution
    alert.resolution_note = body.resolution_note
    db.commit()
    db.refresh(alert)
    logger.info(
        f"[alerts] Alert {alert_id} resolved "
        f"({body.resolution} by {alert.resolved_by})"
    )
    return alert.to_dict()


# ── Dev/test — emit a test alert ───────────────────────────────────────────────

@router.post(
    "/v1/alerts/test",
    status_code=201,
    summary="Emit a test alert (dev only)",
)
def create_test_alert(
    category: str = Query("PROVIDER_UNAVAILABLE"),
    message:  str = Query("Test alert from PAP"),
    db: Session   = Depends(get_db),
):
    alert = PlatformAlert(
        category      = category.upper(),
        severity      = "INFO",
        provider_name = "test",
        payload       = {"message": message, "source": "test_endpoint"},
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return alert.to_dict()
