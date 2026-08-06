"""
Platform Agent Activity emitter (demo).

Renders the GOVERNANCE PLANE itself — the PE gate, Sentinel, and the always-on
specialist detectors — as a live agent graph in the SAME Agent Activity UI used for
the bank app, but under its OWN scope (app_id="trust-platform"). No LangGraph and no
SDK: we just POST the same minimal event contract the SDK's instrument() would emit
(role=agent_to_agent + metadata.{agent_name,source_agent,target_agent}), which the PE
turns into interaction-graph edges and PAP renders by app_id.

Best-effort and OFF by default (PLATFORM_ACTIVITY=true): every call swallows its own
errors so platform-activity emission can NEVER perturb real detection.
"""
import logging
import os
import uuid

import httpx

logger = logging.getLogger("sentinel.platform_activity")

_ENABLED  = os.getenv("PLATFORM_ACTIVITY", "true").strip().lower() in ("1", "true", "yes")
_APP      = os.getenv("PLATFORM_ACTIVITY_APP", "trust-platform")
_PAP_URL  = os.getenv("PAP_URL", "http://127.0.0.1:3002").rstrip("/")

# The standing platform agents shown on the governance-plane graph. The three
# detectors are ALWAYS-ON specialist nodes; "gate" is the PE; recruitment (future)
# would add a coordinator-mediated edge to these same specialist nodes.
GATE_NODE     = "gate"
SENTINEL_NODE = "sentinel"
PLATFORM_AGENTS = [
    GATE_NODE, SENTINEL_NODE,
    "structuring-detector", "collusion-detector", "conflict-detector",
    "policy-reviewer",
    "coordinator",
]


def enabled() -> bool:
    return _ENABLED


async def register(pap_url: str = _PAP_URL) -> None:
    """Register the platform agents under app_id=trust-platform so they pass the PAP
    Agent Activity registry allowlist (unregistered agents are filtered out)."""
    if not _ENABLED:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{pap_url}/internal/register-agents",
                json={"app_id": _APP, "discovered_nodes": PLATFORM_AGENTS,
                      "agent_nodes": PLATFORM_AGENTS, "a2a_tools": {}},
            )
        logger.warning("[platform-activity] registered %d platform agents under app=%s",
                       len(PLATFORM_AGENTS), _APP)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[platform-activity] registration failed (non-fatal): %s", exc)


def outcome_for(determination: str) -> str:
    """Map an agentic detector determination to a Trust-Radar outcome tier."""
    d = str(determination or "").upper()
    if d == "CONFIRMED":
        return "alert"
    if d == "CLEARED":
        return "clear"
    if d == "WATCH":
        return "watch"
    return ""


async def emit(pe_url: str, source_agent: str, target_agent: str,
               content: str, session_id: str = "", outcome: str = "") -> None:
    """Emit one platform-graph edge source_agent -> target_agent via the PE's standard
    evaluate contract, scoped to app_id=trust-platform. `outcome` (alert|watch|clear)
    is the detector's determination, surfaced as the node's tile tier. Best-effort."""
    if not _ENABLED:
        return
    try:
        meta = {"source": _APP, "agent_name": target_agent,
                "source_agent": source_agent, "target_agent": target_agent}
        if outcome:
            meta["platform_outcome"] = outcome
        async with httpx.AsyncClient(timeout=4.0) as client:
            await client.post(
                f"{pe_url.rstrip('/')}/v1/app/evaluate",
                json={
                    "app_id": _APP,
                    "request_id": str(uuid.uuid4()),
                    "role": "agent_to_agent",
                    "content": (content or "")[:300],
                    "eval_mode": "ASYNC",
                    # Stable viz session — the PE exempts the trust-platform scope from all
                    # governance logic (PE_VIZ_APPS), so these viz events can't trip the
                    # breaker/deadlock and don't need throwaway sessions (which churned the
                    # session store and spammed the Eval Trail).
                    "session_id": f"platform-{_APP}",
                    "metadata": meta,
                },
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[platform-activity] emit %s->%s failed (non-fatal): %s",
                     source_agent, target_agent, exc)
