"""
Session-scoped agent/tool blocklist.

Maintained in-memory — survives for the lifetime of the PE process.
Populated by the Sentinel Agent when it detects an ALERT pattern
(e.g. BSA §5324 structuring).  Consulted by the orchestrator on every
evaluate() call before any guardrail work is done.

Blocking an agent is scoped to a single session: other sessions with
the same agent_name are unaffected, and other agents within the blocked
session continue to work normally.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)

# ── Per-session block entry ───────────────────────────────────────────────────

@dataclass
class _BlockEntry:
    blocked_agents: Set[str] = field(default_factory=set)
    blocked_tools:  Set[str] = field(default_factory=set)
    reason:         str      = ""
    blocked_by:     str      = ""


# ── Module-level store ────────────────────────────────────────────────────────

_lock:  threading.Lock               = threading.Lock()
_store: Dict[str, _BlockEntry]       = {}   # session_id → _BlockEntry


# ── Mutators ──────────────────────────────────────────────────────────────────

def block_agent(
    session_id: str,
    agent_name: str,
    reason:     str = "",
    blocked_by: str = "sentinel",
) -> None:
    with _lock:
        entry = _store.setdefault(session_id, _BlockEntry(reason=reason, blocked_by=blocked_by))
        entry.blocked_agents.add(agent_name)
    logger.warning(
        f"[blocklist] agent blocked — session={session_id} "
        f"agent={agent_name} reason={reason!r} by={blocked_by}"
    )


def block_tool(
    session_id: str,
    tool_name:  str,
    reason:     str = "",
    blocked_by: str = "sentinel",
) -> None:
    with _lock:
        entry = _store.setdefault(session_id, _BlockEntry(reason=reason, blocked_by=blocked_by))
        entry.blocked_tools.add(tool_name)
    logger.warning(
        f"[blocklist] tool blocked — session={session_id} "
        f"tool={tool_name} reason={reason!r} by={blocked_by}"
    )


def clear_session_blocks(session_id: str) -> None:
    with _lock:
        _store.pop(session_id, None)
    logger.info(f"[blocklist] blocks cleared for session={session_id}")


def clear_all_blocks() -> None:
    """Clear all session blocks (demo reset — wipes every session's block entries)."""
    with _lock:
        _store.clear()
    logger.info("[blocklist] all session blocks cleared")


# ── Queries ───────────────────────────────────────────────────────────────────

def is_agent_blocked(session_id: str, agent_name: Optional[str]) -> bool:
    if not session_id or not agent_name:
        return False
    with _lock:
        entry = _store.get(session_id)
    return entry is not None and agent_name in entry.blocked_agents


def is_tool_blocked(session_id: str, tool_name: Optional[str]) -> bool:
    if not session_id or not tool_name:
        return False
    with _lock:
        entry = _store.get(session_id)
    return entry is not None and tool_name in entry.blocked_tools


def get_session_blocks(session_id: str) -> dict:
    with _lock:
        entry = _store.get(session_id)
    if entry is None:
        return {"blocked_agents": [], "blocked_tools": [], "reason": "", "blocked_by": ""}
    return {
        "blocked_agents": sorted(entry.blocked_agents),
        "blocked_tools":  sorted(entry.blocked_tools),
        "reason":         entry.reason,
        "blocked_by":     entry.blocked_by,
    }
