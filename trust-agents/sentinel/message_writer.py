"""
Sentinel Message Writer — renders Template B governance messages from SentinelVerdict.

Self-contained: no dependency on policy-engine code.  Imports only from the
sentinel's own schemas.py so the two services remain independently deployable.

Template B — Trust Platform's Sentinel Agent raise
───────────────────────────────────────────────────
  {Role} blocked by Trust Platform's Sentinel Agent      ← ALERT (coercive)
  {Role} referred for human review — Trust Platform's Sentinel Agent  ← WATCH (normative)

  RAI Pillar : {pillar}
  [Pattern   : {pattern}]   ← only when pattern_type is set

  Violation(s):
  • {policy_code}: {description}

  Incident Ref: {ref_id}   ← ALERT
  Review Ref:   {ref_id}   ← WATCH

Sentinel verdict → PE verdict mapping
──────────────────────────────────────
  ALERT → COERCIVE   (blocks the session / agent)
  WATCH → NORMATIVE  (advisory; human review queued)
  CLEAR →  not rendered (no violations)

Usage
─────
    from message_writer import render_sentinel_message

    result = render_sentinel_message(sv, ref_id=sv.session_id[:8])
    # result["coercive"]        — formatted block message (ALERT)
    # result["normative"]       — formatted advisory message (WATCH)
    # result["normative_action"]— "HITL" for all sentinel raises
    # result["template_type"]   — always "B"
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from schemas import SentinelVerdict

logger = logging.getLogger(__name__)

# ── Role labels ───────────────────────────────────────────────────────────────

_ROLE_LABELS: Dict[str, str] = {
    "user_prompt":    "User Prompt",
    "llm_response":   "LLM Response",
    "agent_to_agent": "Agent Message",
    "tool_call":      "Tool Call",
}
_UNKNOWN_ROLE   = "Request"
_SENTINEL_LABEL = "Trust Platform's Sentinel Agent"

# ── Pattern message catalog ───────────────────────────────────────────────────
#
# Keyed by pattern_type (matches _CHECK_PATTERN_MAP in evaluator.py).
# Each entry:
#   policy_code          — TP-SA-NNN identifier
#   coercive_desc        — used in ALERT (blocking) messages
#   normative_desc       — used in WATCH (advisory) messages
#   normative_action     — always "HITL" for sentinel raises
#   normative_guidance   — the "Status:" line in WATCH messages
#
# HITL is mandatory for all sentinel patterns: a cross-turn behavioural pattern
# cannot be self-corrected by the AI; a human operator must review and decide.
#
_PATTERN_MESSAGES: Dict[str, Dict[str, str]] = {

    # ── TP-SA-001: Agent Scope Violation ──────────────────────────────────────
    # Fires on scope_check pre-check. Agent called a tool outside its permitted
    # scope (agent_scopes in policy.yaml).  Always blocking — scope violations
    # carry direct financial risk (e.g. an account agent initiating a transfer).
    "scope_violation": {
        "policy_code":        "TP-SA-001",
        "coercive_desc":      (
            "Agent called a tool outside its permitted scope — "
            "unauthorized access to a restricted tool or service"
        ),
        "normative_desc":     (
            "Potential agent scope boundary probe detected — "
            "tool usage approaching permitted scope limits"
        ),
        "normative_action":   "HITL",
        "normative_guidance": (
            "Session paused for human review. "
            "Verify agent scope configuration and audit all tool calls in this session."
        ),
    },

    # ── TP-SA-002: Agent Coordination Conflict ────────────────────────────────
    # Fires on agent_circular_dispute pre-check. Two agents exchanging messages
    # in both directions within the window — A→B and B→A both present —
    # indicating an unresolved coordination deadlock requiring arbitration.
    "agent_conflict": {
        "policy_code":        "TP-SA-002",
        "coercive_desc":      (
            "Agent coordination deadlock detected — circular A2A message exchange "
            "between agents indicates an unresolved dispute"
        ),
        "normative_desc":     (
            "Persistent cross-agent messaging loop detected — "
            "agent coordination conflict may require human arbitration"
        ),
        "normative_action":   "HITL",
        "normative_guidance": (
            "Session paused for human review. "
            "Human arbitration required to resolve agent coordination deadlock."
        ),
    },

    # ── TP-SA-003: Multi-Agent Collusion ──────────────────────────────────────
    # Fires on a2a_coercive pre-check. Agents passing a harmful plan forward in
    # a linear chain — each individual turn may appear borderline but the
    # accumulated A2A path constitutes a coordinated policy violation.
    "collusion": {
        "policy_code":        "TP-SA-003",
        "coercive_desc":      (
            "Multi-agent collusion detected — agents autonomously coordinating "
            "to execute actions that collectively violate governance policy"
        ),
        "normative_desc":     (
            "Elevated cross-agent coercive activity detected — "
            "agents may be coordinating policy violations across turns"
        ),
        "normative_action":   "HITL",
        "normative_guidance": (
            "Session paused for human review. "
            "Suspend agent-to-agent communication and audit full session history."
        ),
    },

    # ── TP-SA-004: Financial Transaction Structuring ──────────────────────────
    # Fires on domestic_transfer_structuring (amount-based) or permissive_tool_flood
    # (count-based) pre-checks.  The amount-based check is primary for BSA §5324.
    "structuring": {
        "policy_code":        "TP-SA-004",
        "coercive_desc":      (
            "Financial structuring pattern detected — cumulative transaction amounts "
            "structured to evade regulatory reporting thresholds (BSA §5324)"
        ),
        "normative_desc":     (
            "Suspicious transaction structuring pattern emerging — "
            "cumulative transfer amounts approaching regulatory thresholds"
        ),
        "normative_action":   "HITL",
        "normative_guidance": (
            "Session paused for compliance review. "
            "Pause all transaction processing and escalate to compliance team (BSA §5324)."
        ),
    },

    # ── TP-SA-005: Dimensional Escalation ─────────────────────────────────────
    # Fires on dimension_concentration pre-check. The same PE evaluation dimension
    # fires as a violation across 3+ events — indicating a targeted, sustained
    # attack on one governance dimension (e.g. repeated data_leakage probes).
    "dimensional_escalation": {
        "policy_code":        "TP-SA-005",
        "coercive_desc":      (
            "Targeted governance dimension attack detected — repeated violations "
            "concentrated on a single Policy Engine evaluation area across multiple turns"
        ),
        "normative_desc":     (
            "Governance dimension concentration detected — "
            "multiple turns targeting the same policy area may indicate systematic probing"
        ),
        "normative_action":   "HITL",
        "normative_guidance": (
            "Session paused for human review. "
            "Audit session for systematic policy probing and strengthen affected dimension guardrails."
        ),
    },

    # ── TP-SA-006: Jailbreak Enumeration ──────────────────────────────────────
    # Fires on user_coercive_density pre-check. 2+ COERCIVE user_prompt turns in
    # the last 8 turns — the user is systematically probing safety boundaries.
    "enumeration": {
        "policy_code":        "TP-SA-006",
        "coercive_desc":      (
            "Jailbreak enumeration detected — sustained user prompt attacks "
            "attempting to bypass safety guardrails across multiple turns"
        ),
        "normative_desc":     (
            "Repeated jailbreak attempts detected — "
            "user prompt pattern indicates systematic safety boundary probing"
        ),
        "normative_action":   "HITL",
        "normative_guidance": (
            "Session paused for human review. "
            "Apply enhanced content filtering; consider session termination if pattern continues."
        ),
    },

    # ── TP-SA-007: Persistent Violation Pattern ───────────────────────────────
    # Fires on coercive_burst, rapid_escalation, or high_coercive_ratio pre-checks.
    # The session shows sustained high-severity violations — not a one-off event
    # but a consistent escalation trend requiring human assessment.
    "persistence": {
        "policy_code":        "TP-SA-007",
        "coercive_desc":      (
            "Persistent policy violation pattern detected — session exhibits sustained "
            "high-severity violations across multiple turns"
        ),
        "normative_desc":     (
            "Elevated violation frequency detected — "
            "session trend indicates an escalating risk posture"
        ),
        "normative_action":   "HITL",
        "normative_guidance": (
            "Session paused for human review. "
            "Review session history and apply progressive response controls."
        ),
    },

    # ── TP-SA-008: Cross-Border Agent Coordination ────────────────────────────
    # Fires on normative_a2a_pattern pre-check.  2+ NORMATIVE agent_to_agent
    # evaluations in the window — persistent cross-border A2A coordination at
    # NORMATIVE severity.  Non-blocking WATCH advisory alongside HITL referral.
    "cross_border_coordination": {
        "policy_code":        "TP-SA-008",
        "coercive_desc":      (
            "Unauthorized cross-border agent coordination detected — "
            "multi-agent transfer chain bypassing compliance controls"
        ),
        "normative_desc":     (
            "Cross-border agent coordination pattern detected — "
            "multi-step A2A workflow requires compliance officer review"
        ),
        "normative_action":   "HITL",
        "normative_guidance": (
            "Session referred for compliance review. "
            "Pause cross-border transfer workflow and escalate to compliance officer."
        ),
    },
}

# Fallback for unknown / future patterns
_UNKNOWN_PATTERN: Dict[str, str] = {
    "policy_code":        "TP-SA-000",
    "coercive_desc":      "Unclassified behavioural anomaly detected across multiple session turns",
    "normative_desc":     "Unclassified behavioural pattern detected — advisory review recommended",
    "normative_action":   "HITL",
    "normative_guidance": "Session flagged for human review — unclassified cross-turn anomaly.",
}


# ── Output dataclass ──────────────────────────────────────────────────────────

@dataclass
class RenderedSentinelMessage:
    """
    Rendered governance message from the Sentinel.  Mirrors RenderedMessage in
    policy-engine/schemas/response.py without the Pydantic dependency.

    template_type   — always "B" for Sentinel raises.
    normative_action— always "HITL" for Sentinel raises.
    coercive        — set when verdict == ALERT.
    normative       — set when verdict == WATCH.
    """
    coercive:         Optional[str] = None
    normative:        Optional[str] = None
    normative_action: str           = "HITL"   # Sentinel always escalates to HITL
    template_type:    str           = "B"
    rendered_at:      str           = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"
    )

    def as_dict(self) -> dict:
        return {
            "coercive":         self.coercive,
            "normative":        self.normative,
            "normative_action": self.normative_action,
            "template_type":    self.template_type,
            "rendered_at":      self.rendered_at,
        }


# ── Public API ────────────────────────────────────────────────────────────────

def render_sentinel_message(
    sv:   "SentinelVerdict",
    ref_id: Optional[str] = None,
    role:   str           = "agent_to_agent",
) -> RenderedSentinelMessage:
    """
    Build a Template B rendered message from a SentinelVerdict.

    Args:
        sv:     The SentinelVerdict produced by SentinelEvaluator.
        ref_id: Short reference ID (defaults to sv.session_id[:8]).
        role:   Content role triggering the sentinel evaluation.
                Defaults to "agent_to_agent" — the most common sentinel context.

    Returns:
        RenderedSentinelMessage with coercive or normative text populated.
        For CLEAR verdicts, both text fields are None.
    """
    if sv.verdict == "CLEAR":
        return RenderedSentinelMessage()

    try:
        return _render_safe(sv, ref_id or (sv.session_id[:8] if sv.session_id else "unknown"), role)
    except Exception as exc:
        logger.error(f"[SentinelMessageWriter] render failed: {exc} — returning fallback")
        return _fallback(sv, ref_id or "unknown", role)


# ── Internal rendering ────────────────────────────────────────────────────────

def _render_safe(
    sv:     "SentinelVerdict",
    ref_id: str,
    role:   str,
) -> RenderedSentinelMessage:
    pattern  = sv.pattern_type or ""
    msg_tmpl = _PATTERN_MESSAGES.get(pattern, _UNKNOWN_PATTERN)

    policy_code = msg_tmpl["policy_code"]
    role_label  = _ROLE_LABELS.get(role, _UNKNOWN_ROLE)
    pillar      = sv.rai_pillar
    beh_pattern = _pattern_display(pattern)

    if sv.verdict == "ALERT":
        text = _build_coercive(
            role_label, pillar, beh_pattern,
            policy_code, msg_tmpl["coercive_desc"],
            ref_id,
        )
        return RenderedSentinelMessage(coercive=text)

    # WATCH → normative
    norm_guidance = msg_tmpl["normative_guidance"]
    text = _build_normative(
        role_label, pillar, beh_pattern,
        policy_code, msg_tmpl["normative_desc"],
        norm_guidance, ref_id,
    )
    return RenderedSentinelMessage(normative=text)


def _build_coercive(
    role_label:  str,
    pillar:      Optional[str],
    beh_pattern: Optional[str],
    policy_code: str,
    description: str,
    ref_id:      str,
) -> str:
    header = _agent_header(role_label, "blocked by", pillar, beh_pattern)
    bullet = f"• {policy_code}: {description}"
    return (
        f"{header}"
        f"Violation(s):\n{bullet}\n\n"
        f"Incident Ref: {ref_id}"
    )


def _build_normative(
    role_label:  str,
    pillar:      Optional[str],
    beh_pattern: Optional[str],
    policy_code: str,
    description: str,
    guidance:    str,
    ref_id:      str,
) -> str:
    header = _agent_header(role_label, "referred for human review —", pillar, beh_pattern)
    bullet = f"• {policy_code}: {description}"
    return (
        f"{header}"
        f"{bullet}\n\n"
        f"Status: {guidance}\n\n"
        f"Review Ref: {ref_id}"
    )


def _agent_header(
    role_label:  str,
    action:      str,
    pillar:      Optional[str],
    beh_pattern: Optional[str],
) -> str:
    """
    Build the opening block for all Template B Sentinel messages.

    Example:
        Agent Message blocked by Trust Platform's Sentinel Agent

        RAI Pillar : Accountability
        Pattern    : Multi-Agent Collusion

    The trailing blank line separates the header from the violation bullet.
    """
    lines = [f"{role_label} {action} {_SENTINEL_LABEL}", ""]
    if pillar:
        lines.append(f"RAI Pillar : {pillar}")
    if beh_pattern:
        lines.append(f"Pattern    : {beh_pattern}")
    lines.append("")   # blank line before violation bullet
    return "\n".join(lines)


def _pattern_display(pattern_type: str) -> Optional[str]:
    """Convert snake_case pattern_type to a human-readable display label."""
    _DISPLAY: Dict[str, str] = {
        "scope_violation":          "Agent Scope Violation",
        "agent_conflict":           "Agent Coordination Conflict",
        "collusion":                "Multi-Agent Collusion",
        "structuring":              "Financial Transaction Structuring",
        "dimensional_escalation":   "Targeted Dimension Escalation",
        "enumeration":              "Jailbreak Enumeration",
        "persistence":              "Persistent Violation Pattern",
        "cross_border_coordination":"Cross-Border Agent Coordination",
    }
    return _DISPLAY.get(pattern_type)


def _fallback(
    sv:     "SentinelVerdict",
    ref_id: str,
    role:   str,
) -> RenderedSentinelMessage:
    role_label = _ROLE_LABELS.get(role, _UNKNOWN_ROLE)
    if sv.verdict == "ALERT":
        return RenderedSentinelMessage(
            coercive=(
                f"{role_label} blocked by {_SENTINEL_LABEL}\n\n"
                f"Incident Ref: {ref_id}"
            )
        )
    return RenderedSentinelMessage(
        normative=(
            f"{role_label} referred for human review — {_SENTINEL_LABEL}\n\n"
            f"Review Ref: {ref_id}"
        )
    )
