"""
Policy Engine inbound request schema.
Matches SRS §4.5 PolicyEvaluateRequest.
"""
from enum import Enum
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field


class ContentRole(str, Enum):
    USER_PROMPT      = "user_prompt"
    LLM_RESPONSE     = "llm_response"
    AGENT_TO_AGENT   = "agent_to_agent"
    TOOL_CALL        = "tool_call"       # Phase 4+ — pre/post tool execution
    HITL_CHECKPOINT  = "hitl_checkpoint" # governance pause event (initiated/decision/resolved)
    # HITL_CHECKPOINT records are pass-through: the PE writes the audit row
    # but skips guardrail evaluation and returns PERMISSIVE unconditionally.
    # They MUST NOT increment synthetic turn counters in the dashboard.


class EvalMode(str, Enum):
    SYNC  = "SYNC"   # Block until verdict is returned
    ASYNC = "ASYNC"  # Fire-and-forget; verdict logged but not enforced


class PolicyEvaluateRequest(BaseModel):
    app_id:     str         = Field(..., description="Application identifier (scopes policy lookup)")
    request_id: str         = Field(..., description="OTel Trace ID — echoed in response")
    role:       ContentRole = Field(..., description="user_prompt | llm_response | agent_to_agent | tool_call")
    content:    str         = Field(..., description="Post-Privacy Proxy sanitised content")

    # Phase 4 — multi-turn session correlation (SRS §4.5.5)
    session_id: Optional[str] = Field(
        None,
        description=(
            "Groups turns into a multi-turn workflow session. "
            "When supplied the Policy Engine accumulates per-dimension scores "
            "across turns and may escalate the verdict beyond the per-turn result. "
            "Omit for single-turn (stateless) evaluations."
        ),
    )

    agent_id:  Optional[str] = Field(None, description="NHI-verified agent identifier (optional)")
    eval_mode: EvalMode      = Field(EvalMode.SYNC, description="SYNC (default) or ASYNC")

    # Explicit turn number from the caller (SDK or showcase runner).
    # When present the PE uses this value directly instead of its own self-maintained
    # counter (state.turn_count).  This transfers turn-count ownership to the caller,
    # which is always the authoritative source of truth about conversation structure.
    # Falls back to the PE's internal counter when absent (backwards-compatible).
    turn_number: Optional[int] = Field(
        None,
        description=(
            "Caller-supplied turn number. Overrides the PE's own turn counter when present. "
            "The SDK stamps this on every evaluate() call; showcase runners set it explicitly. "
            "Omit to use the PE's self-maintained counter (legacy / SDK-less callers)."
        ),
    )

    metadata:  Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Optional caller metadata — written to audit trail only, not used for verdict logic. "
            "For tool_call role: include tool_name, tool_phase (pre|post), source_agent, target_agent."
        ),
    )
