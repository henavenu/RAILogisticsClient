"""
Core types for the RAI Trust SDK.

These mirror the Policy Engine's request/response schemas (SRS §4.5) so the
SDK has no runtime dependency on the PE codebase.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Enums ─────────────────────────────────────────────────────────────────────

class ContentRole(str, Enum):
    """Content classification for governance evaluation."""
    USER_PROMPT     = "user_prompt"
    LLM_RESPONSE    = "llm_response"
    AGENT_TO_AGENT  = "agent_to_agent"
    TOOL_CALL       = "tool_call"       # Phase 4+ — pre/post tool execution
    HITL_CHECKPOINT = "hitl_checkpoint" # governance pause event — pass-through, not evaluated


class EvalMode(str, Enum):
    """Evaluation execution mode."""
    SYNC  = "SYNC"   # Block until verdict returned
    ASYNC = "ASYNC"  # Fire-and-log; verdict never blocks execution


class EnforcementMode(str, Enum):
    """
    Controls how the SDK acts on a COERCIVE policy verdict.

    NONE
        Log the result, then continue execution unchanged.
        Use for pure observation / shadow mode — governance has no runtime effect.

    REDACT  *(default)*
        Substitute safe content for COERCIVE verdicts without raising an exception.
        Layer 1 (USER_PROMPT): graph is not invoked; a policy-safe refusal is returned.
        Layer 2 (TOOL_CALL): tool is blocked; a safe refusal is returned as the reply.
        Layer 3 (LLM_RESPONSE): the LLM output is replaced with a safe refusal.
        Applications need zero error-handling code.

    RAISE
        Raise ``GovernanceBlock`` on COERCIVE verdicts.
        Opt-in for applications that want rich exception data (violations list,
        verdict, request_id) to build custom UI responses such as flagging the
        blocked action in a chat widget.
    """
    NONE   = "NONE"
    REDACT = "REDACT"
    RAISE  = "RAISE"


class Verdict(str, Enum):
    """Policy verdict returned by the Trust Platform."""
    PERMISSIVE = "PERMISSIVE"  # All evaluations within safe band — proceed
    NORMATIVE  = "NORMATIVE"   # One or more warnings — proceed with caution
    COERCIVE   = "COERCIVE"    # Policy violation — block in SYNC mode


# ── Directive taxonomy ─────────────────────────────────────────────────────────
#
# Directives are *actionable instructions* from the PE to the SDK.
# Distinct from verdicts (which describe what was detected).
# The SDK processes directives after receiving a GovernanceResult — these are
# mirrored from policy-engine/schemas/response.py and must stay in sync.

class DirectiveAction(str, Enum):
    """Action the SDK must take upon receiving a directive."""
    REDACT               = "redact"
    REQUIRE_HUMAN_REVIEW = "require_human_review"


class HITLCategory(str, Enum):
    """
    Category of human review required.
    Drives which reviewer options the HITL UI presents.
    """
    AGENT_ARBITRATION = "agent_arbitration"
    # Deadlocked multi-agent negotiation (propose-and-dispose pattern).

    SANCTIONS_REVIEW  = "sanctions_review"
    # AML/sanctions flag requiring compliance sign-off.


class DirectiveSource(str, Enum):
    """Component that issued the directive."""
    PE          = "pe"
    SENTINEL    = "sentinel"
    COORDINATOR = "coordinator"


# Default reviewer options per HITL category.
HITL_DEFAULT_OPTIONS: Dict[str, List[str]] = {
    HITLCategory.AGENT_ARBITRATION.value: [
        "approve_recommendation",
        "select_alternative",
        "reject_all",
        "request_more_information",
        "escalate_to_advisor",
    ],
    HITLCategory.SANCTIONS_REVIEW.value: [
        "approve",
        "approve_with_waiver",
        "temporary_exemption",
        "reject",
        "update_policy",
        "escalate_to_compliance",
        "file_sar",
    ],
}


class Directive(BaseModel):
    """
    Structured, actionable instruction from the PE (or Sentinel via PE) to the SDK.

    The SDK processes directives in priority order after receiving GovernanceResult:
        REQUIRE_HUMAN_REVIEW  →  raise HITLInterrupt (highest priority)
        REDACT                →  substitute response with response_text
    """
    action:   DirectiveAction
    category: Optional[HITLCategory] = None
    reason:   str                    = ""
    options:  List[str]              = Field(default_factory=list)
    source:   DirectiveSource        = DirectiveSource.PE
    metadata: Dict[str, Any]         = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """Auto-populate reviewer options from defaults when not explicitly set."""
        if (
            self.action == DirectiveAction.REQUIRE_HUMAN_REVIEW
            and not self.options
            and self.category is not None
            and self.category.value in HITL_DEFAULT_OPTIONS
        ):
            # Pydantic v2: lists are mutable post-init
            object.__setattr__(
                self, "options",
                list(HITL_DEFAULT_OPTIONS[self.category.value]),
            )


# Priority order for SDK directive processing (highest first)
_DIRECTIVE_PRIORITY: Dict[DirectiveAction, int] = {
    DirectiveAction.REQUIRE_HUMAN_REVIEW: 10,
    DirectiveAction.REDACT:               5,
}


# ── Exception ─────────────────────────────────────────────────────────────────

class GovernanceBlock(Exception):
    """
    Raised in SYNC mode when the Policy Engine returns a COERCIVE verdict.

    Propagates naturally through both LangChain chains and LangGraph graph
    execution, terminating the run without requiring explicit check-and-raise
    boilerplate in application code.

    Attributes:
        result: The full GovernanceResult that triggered the block.
    """
    def __init__(self, result: GovernanceResult):
        self.result = result
        violations = "; ".join(result.violations) if result.violations else "policy violation"
        super().__init__(
            f"[RAI Trust] COERCIVE verdict — {violations} "
            f"(request_id={result.request_id})"
        )


class HITLInterrupt(Exception):
    """
    Raised when a PE directive requires human-in-the-loop (HITL) review.

    Triggered when the Policy Engine returns a ``Directive`` with
    ``action=REQUIRE_HUMAN_REVIEW`` in the evaluation response.  Unlike
    ``GovernanceBlock`` (which terminates on COERCIVE verdicts), HITLInterrupt
    signals a governance pause — execution is held pending a reviewer decision
    via the /api/hitl/resolve endpoint.

    Propagates through LangGraph node execution the same way GovernanceBlock
    does, allowing GovernedGraph and the application server to catch it and
    implement the HITL workflow.

    Attributes:
        payload: Context dict with keys:
            reason      — operator-facing explanation from the Directive
            category    — HITLCategory value (e.g. "agent_arbitration")
            options     — reviewer choices to present in the HITL UI
            source      — who issued the directive ("pe" | "sentinel" | "coordinator")
            session_id  — session the review belongs to
            directive   — full Directive model_dump() for downstream consumers
    """
    def __init__(self, payload: dict):
        self.payload = payload
        super().__init__(
            f"[RAI Trust] HITL required — {payload.get('reason', 'governance_review')} "
            f"(session={payload.get('session_id', '?')} "
            f"category={payload.get('category', '?')} "
            f"source={payload.get('source', '?')})"
        )


# ── Response models ────────────────────────────────────────────────────────────

class FailedEvaluation(BaseModel):
    """Per-dimension detail for non-permissive results."""
    dimension: str
    score:     Optional[int] = None
    status:    str                       # WARNING | FLAGGED
    provider:  Optional[str] = None


class PolicyResult(BaseModel):
    """Per-policy verdict detail."""
    policy_name: str
    score:       Optional[int] = None
    verdict:     Verdict
    severity:    int                     # 1 (low) – 3 (high)
    remediation: Optional[str] = None


class SessionSummary(BaseModel):
    """
    Multi-turn session trust snapshot.

    Attached to every response when a session_id is present.
    composite_score: 1.0 = fully trusted, 0.0 = maximum risk.
    """
    turn_count:       int
    composite_score:  float              # 0.0 – 1.0
    worst_verdict:    str
    dimension_scores: Dict[str, int]     # worst raw score (1-9) per dimension
    escalated:        bool


class GovernanceResult(BaseModel):
    """Full response from POST /v1/app/evaluate."""
    request_id:         str
    verdict:            Verdict
    violations:         List[str]              = Field(default_factory=list)
    # DEPRECATED: kept for one release cycle; use response_text + directives instead.
    remediations:       List[str]              = Field(default_factory=list)
    response_text:      str                    = ""
    # Human-readable text the SDK should present when a REDACT directive is present.
    directives:         List[Directive]        = Field(default_factory=list)
    # Actionable SDK instructions processed in priority order via _DIRECTIVE_PRIORITY.
    failed_evaluations: List[FailedEvaluation] = Field(default_factory=list)
    policies:           List[PolicyResult]     = Field(default_factory=list)
    session_summary:    Optional[SessionSummary] = None
    pack_trace:         List[Dict[str, Any]]   = Field(default_factory=list)
