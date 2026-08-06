"""
SentinelSyncClient — synchronous HTTP client for the Sentinel /evaluate-sync endpoint.

Called by the PolicyOrchestrator when the A2A message accumulator reaches the
configured cap (default 6 messages = 3 full back-and-forth cycles).

Design
------
• Synchronous httpx (blocking) — fits the existing evaluate() call-graph without
  requiring an async refactor.
• Fail-open on timeout or any HTTP/network error — PERMISSIVE is returned so that
  a Sentinel outage never blocks a production evaluation path.
• No VerdictStore write, no Trust Radar post — this client is purely evaluator mode.
  The async observer path (SentinelWebhookForwarder) remains completely decoupled.

Configuration
-------------
  SENTINEL_SYNC_URL      Base URL of the Sentinel service, e.g. http://127.0.0.1:3012
                         When absent the orchestrator receives None and skips A2A checks.
  A2A_HITL_THRESHOLD     Number of accumulated A2A messages before sync eval fires.
                         Read in main.py; the default is 6 (3 full cycles).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class SentinelSyncClient:
    """
    Thin synchronous wrapper around Sentinel's POST /evaluate-sync endpoint.

    Parameters
    ----------
    url         Base URL of the Sentinel service (trailing slash stripped).
    timeout     Request timeout in seconds.  Default 8 s — generous enough for
                a local LLM call, tight enough to not stall a production request.
    """

    def __init__(self, url: str, timeout: float = 8.0) -> None:
        self._url     = url.rstrip("/")
        self._timeout = timeout
        logger.info(
            f"[SentinelSyncClient] Initialised — "
            f"url={self._url}  timeout={self._timeout}s"
        )

    # ── Public interface ───────────────────────────────────────────────────────

    def evaluate_sync(
        self,
        session_id:       str,
        a2a_messages:     List[str],
        proposed_options: Optional[List[dict]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        POST /evaluate-sync and return the parsed JSON response dict.

        Returns
        -------
        dict  with keys: verdict, pattern_type, reasoning, recommended_option
              (all fields optional except verdict)
        None  on any network / HTTP error or timeout — caller treats as PERMISSIVE.
        """
        payload: Dict[str, Any] = {
            "session_id":   session_id,
            "a2a_messages": a2a_messages,
        }
        if proposed_options is not None:
            payload["proposed_options"] = proposed_options

        endpoint = f"{self._url}/evaluate-sync"
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(endpoint, json=payload)
                resp.raise_for_status()
                result = resp.json()
                logger.info(
                    f"[SentinelSyncClient] evaluate_sync — "
                    f"session={session_id}  "
                    f"verdict={result.get('verdict', '?')}  "
                    f"pattern={result.get('pattern_type', 'none')}"
                )
                return result
        except httpx.TimeoutException:
            logger.warning(
                f"[SentinelSyncClient] Timeout after {self._timeout}s "
                f"for session={session_id} — failing open (PERMISSIVE)"
            )
            return None
        except Exception as exc:
            logger.warning(
                f"[SentinelSyncClient] Error calling {endpoint} "
                f"for session={session_id}: {exc} — failing open (PERMISSIVE)"
            )
            return None

    def detect_sync(
        self,
        risk_class: str,
        stream:     List[Any],
        budget_ms:  float,
    ) -> Optional[Dict[str, Any]]:
        """
        Time-boxed inline agentic adjudication for a deterministic WATCH (Phase 3).

        POST /internal/detect-sync with a HARD timeout of budget_ms. Runs the
        Phase-0 specialist detector for risk_class over `stream` (payments for
        structuring, A2A messages for collusion/conflict) and returns:

          {"determination": "CONFIRMED"|"CLEARED"|"?", "reason": str, "latency_ms": float}

        Returns None on timeout / error — the caller applies its TIER-STRICT cold-start
        fallback (fail-closed for high-risk classes, fail-open for low-risk). The tight
        budget is the whole point: an over-sensitive gate is only acceptable if the agent
        can uphold/decline within it (Part IV §C).
        """
        endpoint = f"{self._url}/internal/detect-sync"
        timeout_s = max(0.1, float(budget_ms) / 1000.0)
        try:
            with httpx.Client(timeout=timeout_s) as client:
                resp = client.post(endpoint, json={"risk_class": risk_class, "stream": stream})
                resp.raise_for_status()
                result = resp.json()
                logger.info(
                    f"[SentinelSyncClient] detect_sync — class={risk_class} "
                    f"determination={result.get('determination', '?')} "
                    f"latency_ms={result.get('latency_ms', '?')}"
                )
                return result
        except httpx.TimeoutException:
            logger.warning(
                f"[SentinelSyncClient] detect_sync TIMEOUT after {timeout_s:.2f}s "
                f"(class={risk_class}) — caller applies tier-strict fallback"
            )
            return None
        except Exception as exc:
            logger.warning(
                f"[SentinelSyncClient] detect_sync error (class={risk_class}): {exc} "
                f"— caller applies tier-strict fallback"
            )
            return None
