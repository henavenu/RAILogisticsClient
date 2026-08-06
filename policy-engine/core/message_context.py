"""
MessageWriter context types — shared by PE engine and Trust Agent writers.

Intentionally has no dependency on the PE DB, Pydantic, or any service
infrastructure so it can be imported by both the Policy Engine and the
Sentinel / Coordination Agent message writers without circular imports.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


# ── Role labels ───────────────────────────────────────────────────────────────

class ContentRole(str, Enum):
    """
    Mirrors policy-engine/schemas/request.py ContentRole exactly.
    Redefined here so message_context has zero upstream imports.
    """
    USER_PROMPT    = "user_prompt"
    LLM_RESPONSE   = "llm_response"
    AGENT_TO_AGENT = "agent_to_agent"
    TOOL_CALL      = "tool_call"
    # TOOL_RESPONSE  — Phase 5 addition; reserved but not yet in PE schema


ROLE_LABELS: dict[str, str] = {
    "user_prompt":    "User Prompt",
    "llm_response":   "LLM Response",
    "agent_to_agent": "Agent Message",
    "tool_call":      "Tool Call",
    # Phase 5: "tool_response": "Tool Response",
}


# ── Raising-entity labels ─────────────────────────────────────────────────────

class RaisingEntity(str, Enum):
    TRUST_PLATFORM     = "pe"
    SENTINEL_AGENT     = "sentinel"
    COORDINATION_AGENT = "coordinator"


ENTITY_LABELS: dict[str, str] = {
    "pe":          "Trust Platform",
    "sentinel":    "Trust Platform's Sentinel Agent",
    "coordinator": "Trust Platform's Coordination Agent",
}


# ── Normative action ──────────────────────────────────────────────────────────

class NormativeAction(str, Enum):
    HITL     = "HITL"      # Pause session → human review queue
    REVISION = "REVISION"  # AI/agent must self-correct before proceeding


# ── Input types ───────────────────────────────────────────────────────────────

@dataclass
class PolicyViolation:
    """
    One resolved policy violation — all content fields pre-loaded from
    PolicyDefinition (PE) or PATTERN_MESSAGES (Sentinel/Coordinator).
    The MessageWriter never touches the DB.
    """
    policy_code:           str            # TP-CS-001 etc.
    normative_description: str            # "what was detected" — normative tone
    coercive_description:  str            # "what was detected" — coercive tone
    normative_action:      NormativeAction
    normative_guidance:    str            # revision instruction OR HITL status line


@dataclass
class EvaluationContext:
    """Runtime context injected by the caller at evaluation time."""
    role:               str              # ContentRole.value
    ref_id:             str              # first 8 chars of request_id
    raising_entity:     RaisingEntity
    rai_pillar:         Optional[str] = None   # Template B only
    behavioral_pattern: Optional[str] = None   # Template B, optional
