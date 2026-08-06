"""
SentinelWebhookForwarder — fires an async webhook to the Sentinel Agent
when a governance trigger condition is met.

Trigger conditions (OR):
  1. Final verdict == COERCIVE  — immediate high-severity event
  2. turn_number % N == 0       — periodic N-turn blind-spot scan
     (N is configurable via SENTINEL_TRIGGER_N, default 5)

The webhook is dispatched in a background daemon thread so it is
completely non-blocking — a slow or unavailable Sentinel service
never degrades the evaluation response returned to the SDK caller.

All exceptions are logged and swallowed (same isolation pattern as
HitlAlertWriter and SqliteAuditWriter).

Webhook payload (POST application/json):
  {
    "interaction_id" : "<uuid>",
    "session_id"     : "<uuid | null>",
    "app_id"         : "<str | null>",
    "role"           : "user_prompt | llm_response | tool_call | ...",
    "verdict"        : "PERMISSIVE | NORMATIVE | COERCIVE",
    "violations"     : ["...", ...],
    "turn_number"    : <int | null>,
    "trigger_reason" : "COERCIVE | N_TURN",
    "fired_at"       : "<ISO-8601 UTC milliseconds>"
  }

The Sentinel service is expected to respond with HTTP 2xx.  Any
non-2xx or connection error is logged at WARNING level and dropped.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

# Default connect + read timeout for the webhook POST (seconds).
# Keep short — Sentinel should acknowledge quickly; actual analysis is async.
_DEFAULT_TIMEOUT = 3.0


class SentinelWebhookForwarder:
    """
    Thread-safe, fire-and-forget HTTP webhook emitter for the Sentinel Agent.

    Each call to :meth:`emit` dispatches a daemon thread that performs the
    POST so the PE evaluation pipeline is never blocked waiting for Sentinel.

    Parameters
    ----------
    webhook_url : str
        Full URL of the Sentinel ``POST /evaluate`` endpoint.
        Example: ``http://localhost:3005/evaluate``
    timeout : float
        Combined connect + read timeout for the POST request (default 3 s).
    """

    def __init__(self, webhook_url: str, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self._webhook_url = webhook_url.rstrip("/")
        self._timeout     = timeout
        logger.info(
            f"[SentinelWebhookForwarder] Initialised — "
            f"URL: {self._webhook_url}  timeout: {self._timeout}s"
        )

    # ── Public ─────────────────────────────────────────────────────────────────

    def emit(
        self,
        *,
        interaction_id: str,
        session_id:     Optional[str],
        app_id:         Optional[str],
        role:           str,
        verdict:        str,
        violations:     List[str],
        turn_number:    Optional[int],
        trigger_reason: str,   # "COERCIVE" | "N_TURN"
    ) -> None:
        """
        Build a webhook payload and dispatch it in a background daemon thread.

        Returns immediately — caller is never blocked.  Never raises.

        Parameters
        ----------
        interaction_id  Unique evaluation ID (from the orchestrator).
        session_id      Active session UUID, or None for stateless requests.
        app_id          Application identifier (e.g. ``"mybank"``).
        role            Content role string (``"user_prompt"``, ``"llm_response"``, …).
        verdict         Final verdict string (``"PERMISSIVE"``, ``"NORMATIVE"``, ``"COERCIVE"``).
        violations      Human-readable violation strings from the evaluation result.
        turn_number     Monotonically increasing turn counter within the session (may be None
                        for stateless requests — Sentinel will skip N-turn analysis in that case).
        trigger_reason  Why this webhook fired: ``"COERCIVE"`` or ``"N_TURN"``.
        """
        payload = {
            "interaction_id": interaction_id,
            "session_id":     session_id,
            "app_id":         app_id,
            "role":           role,
            "verdict":        verdict,
            "violations":     violations,
            "turn_number":    turn_number,
            "trigger_reason": trigger_reason,
            "fired_at":       datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        }

        t = threading.Thread(
            target=self._post,
            args=(payload,),
            daemon=True,
            name=f"sentinel-webhook-{interaction_id[:8]}",
        )
        t.start()
        logger.debug(
            f"[SentinelWebhookForwarder] Webhook dispatched — "
            f"interaction={interaction_id[:8]} "
            f"trigger={trigger_reason} "
            f"verdict={verdict} "
            f"turn={turn_number}"
        )

    # ── Internal ───────────────────────────────────────────────────────────────

    def _post(self, payload: dict) -> None:
        """Perform the HTTP POST.  Called in a daemon thread — never raises."""
        interaction_id = payload.get("interaction_id", "?")[:8]
        try:
            resp = httpx.post(
                self._webhook_url,
                json=payload,
                timeout=self._timeout,
                headers={"Content-Type": "application/json"},
            )
            if resp.is_success:
                logger.info(
                    f"[SentinelWebhookForwarder] OK {resp.status_code} — "
                    f"interaction={interaction_id} "
                    f"trigger={payload.get('trigger_reason')} "
                    f"verdict={payload.get('verdict')}"
                )
            else:
                logger.warning(
                    f"[SentinelWebhookForwarder] Non-2xx {resp.status_code} — "
                    f"interaction={interaction_id} "
                    f"body={resp.text[:200]!r}"
                )
        except httpx.TimeoutException:
            logger.warning(
                f"[SentinelWebhookForwarder] Timeout after {self._timeout}s — "
                f"interaction={interaction_id} url={self._webhook_url}"
            )
        except Exception as exc:
            logger.warning(
                f"[SentinelWebhookForwarder] POST failed — "
                f"interaction={interaction_id} error={exc}"
            )
