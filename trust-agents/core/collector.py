"""
Collector — fetches T1, T2, and session metrics from PAP.

All calls are fire-and-forget-safe: failures return empty dicts so the agent
can still run drift detection and LLM synthesis with whatever data is available.
"""
from __future__ import annotations

import logging
import os
import sys

import httpx

logger = logging.getLogger(__name__)

# REQUIRED — the Trust Agents Service collects snapshots from PAP; no silent
# localhost fallback (a dead-port default would just disguise a missing config).
_PAP_URL = os.getenv("PAP_URL", "").strip()
if not _PAP_URL:
    sys.stderr.write(
        "\nFATAL [Trust Agents]: required env 'PAP_URL' is not set — the collector "
        "pulls snapshots from PAP. Refusing to start (no localhost fallback).\n\n"
    )
    raise SystemExit(1)
_TIMEOUT = float(os.getenv("COLLECT_TIMEOUT_SECONDS", "10"))


async def fetch_t1(window_minutes: int = 60) -> dict:
    """Fetch T1 pillar compliance metrics from PAP /v1/metrics/tier1."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(
                f"{_PAP_URL}/v1/metrics/tier1",
                params={"window_minutes": window_minutes},
            )
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        logger.warning(f"[Collector] T1 fetch failed: {exc}")
        return {}


async def fetch_t2() -> dict:
    """Fetch T2 assurance / quality metrics from PAP /v1/assurance/metrics."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{_PAP_URL}/v1/assurance/metrics")
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        logger.warning(f"[Collector] T2 fetch failed: {exc}")
        return {}


async def fetch_sessions(window_hours: int = 1) -> dict:
    """
    Fetch recent session summaries from PAP /v1/sessions/summaries.
    Returns pe_session_summaries data: coercive_count, final_trust_score, turn_count.
    Endpoint is optional — returns empty dict if not yet implemented.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(
                f"{_PAP_URL}/v1/sessions/summaries",
                params={"window_hours": window_hours},
            )
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        logger.debug(f"[Collector] Session fetch failed (optional): {exc}")
        return {}
