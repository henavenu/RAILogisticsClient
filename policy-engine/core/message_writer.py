"""
MessageWriter — assembles the final governance message from policy violations
and runtime evaluation context.

Owns the template structure; callers supply content and context only.
Never queries the DB — all policy data arrives pre-resolved via PolicyViolation.
Never raises — every failure returns a safe fallback message.

Template A — Trust Platform (Policy Engine raise)
─────────────────────────────────────────────────
  {Role} blocked by Trust Platform                          ← coercive
  {Role} flagged — revision required by Trust Platform      ← normative REVISION
  {Role} referred for human review — Trust Platform         ← normative HITL

  Violation(s):
  • {policy_code}: {description}

  Incident Ref: {ref_id}  |  Review Ref: {ref_id}

Template B — Trust Agent raise (Sentinel / Coordination Agent)
──────────────────────────────────────────────────────────────
  {Role} blocked by Trust Platform's Sentinel Agent
  ...same structure plus:

  RAI Pillar : {rai_pillar}
  [Pattern   : {behavioral_pattern}]   ← rendered only when present

Side-effects
────────────
  None.  HITL alert writing is the caller's responsibility (MRA / Sentinel).
  The writer signals HITL via RenderedMessage.normative_action == 'HITL'.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from core.message_context import (
    EvaluationContext,
    NormativeAction,
    PolicyViolation,
    ROLE_LABELS,
    ENTITY_LABELS,
)
from schemas.response import RenderedMessage

logger = logging.getLogger(__name__)

# Fallback role / entity labels when the value is not in the lookup table
_UNKNOWN_ROLE   = "Request"
_UNKNOWN_ENTITY = "Trust Platform"


class MessageWriter:
    """
    Stateless message renderer.  Safe to share as a module-level singleton.

    Usage (MRA / ChainExecutor)
    ───────────────────────────
        _writer = MessageWriter()

        rendered = _writer.render(
            violations = pv_list,           # List[PolicyViolation]
            context    = EvaluationContext(
                role           = request.role.value,
                ref_id         = request_id[:8],
                raising_entity = RaisingEntity.TRUST_PLATFORM,
            ),
            verdict    = final_verdict.value,  # "NORMATIVE" | "COERCIVE"
        )
        # rendered.coercive  — formatted block message (COERCIVE)
        # rendered.normative — formatted advisory / HITL message (NORMATIVE)
        # rendered.normative_action — "HITL" | "REVISION"
        # rendered.template_type   — "A" | "B"
    """

    # ── Public API ────────────────────────────────────────────────────────────

    def render(
        self,
        violations: List[PolicyViolation],
        context:    EvaluationContext,
        verdict:    str,
    ) -> RenderedMessage:
        """
        Build and return the fully-rendered RenderedMessage.

        verdict must be 'NORMATIVE' or 'COERCIVE'.
        PERMISSIVE results should not be passed to the writer — an empty
        RenderedMessage is returned in that case as a safety guard.
        """
        if not violations or verdict == "PERMISSIVE":
            return RenderedMessage()

        try:
            return self._render_safe(violations, context, verdict)
        except Exception as exc:
            logger.error(f"[MessageWriter] render failed: {exc} — returning fallback")
            return self._fallback(context, verdict)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _render_safe(
        self,
        violations: List[PolicyViolation],
        context:    EvaluationContext,
        verdict:    str,
    ) -> RenderedMessage:
        is_agent_raise = context.raising_entity.value in ("sentinel", "coordinator")
        template_type  = "B" if is_agent_raise else "A"

        coercive_text  = None
        normative_text = None
        norm_action    = None

        if verdict == "COERCIVE":
            coercive_text = self._build_coercive(violations, context, template_type)

        elif verdict == "NORMATIVE":
            normative_text, norm_action = self._build_normative(
                violations, context, template_type
            )

        return RenderedMessage(
            coercive        = coercive_text,
            normative       = normative_text,
            normative_action= norm_action.value if norm_action else None,
            template_type   = template_type,
        )

    # ── Template A / B — coercive ─────────────────────────────────────────────

    def _build_coercive(
        self,
        violations:    List[PolicyViolation],
        ctx:           EvaluationContext,
        template_type: str,
    ) -> str:
        role_label   = ROLE_LABELS.get(ctx.role,               _UNKNOWN_ROLE)
        entity_label = ENTITY_LABELS.get(ctx.raising_entity.value, _UNKNOWN_ENTITY)
        bullets      = self._bullet_list(
            [(v.policy_code, v.coercive_description) for v in violations]
        )

        if template_type == "A":
            return (
                f"{role_label} blocked by {entity_label}\n\n"
                f"Violation(s):\n{bullets}\n\n"
                f"Incident Ref: {ctx.ref_id}"
            )

        # Template B — prepend RAI Pillar block
        header = self._agent_header(role_label, entity_label, ctx, action="blocked")
        return f"{header}Violation(s):\n{bullets}\n\nIncident Ref: {ctx.ref_id}"

    # ── Template A / B — normative ────────────────────────────────────────────

    def _build_normative(
        self,
        violations:    List[PolicyViolation],
        ctx:           EvaluationContext,
        template_type: str,
    ) -> tuple[str, NormativeAction]:
        role_label   = ROLE_LABELS.get(ctx.role,               _UNKNOWN_ROLE)
        entity_label = ENTITY_LABELS.get(ctx.raising_entity.value, _UNKNOWN_ENTITY)

        # Primary violation drives the action type
        primary = violations[0]
        action  = primary.normative_action

        bullets = self._bullet_list(
            [(v.policy_code, v.normative_description) for v in violations]
        )

        if action == NormativeAction.REVISION:
            if template_type == "A":
                msg = (
                    f"{role_label} flagged — revision required by {entity_label}\n\n"
                    f"{bullets}\n"
                    f"Guidance: {primary.normative_guidance}\n\n"
                    f"Review Ref: {ctx.ref_id}"
                )
            else:
                header = self._agent_header(
                    role_label, entity_label, ctx, action="flagged — revision required by"
                )
                msg = (
                    f"{header}"
                    f"{bullets}\n"
                    f"Guidance: {primary.normative_guidance}\n\n"
                    f"Review Ref: {ctx.ref_id}"
                )
        else:  # HITL
            if template_type == "A":
                msg = (
                    f"{role_label} referred for human review — {entity_label}\n\n"
                    f"{bullets}\n"
                    f"Status: {primary.normative_guidance}\n\n"
                    f"Review Ref: {ctx.ref_id}"
                )
            else:
                header = self._agent_header(
                    role_label, entity_label, ctx, action="referred for human review —"
                )
                msg = (
                    f"{header}"
                    f"{bullets}\n"
                    f"Status: {primary.normative_guidance}\n\n"
                    f"Review Ref: {ctx.ref_id}"
                )

        return msg, action

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _agent_header(
        role_label:   str,
        entity_label: str,
        ctx:          EvaluationContext,
        action:       str,
    ) -> str:
        """
        Build the opening block for Template B messages.

        Example output:
            Agent Message blocked by Trust Platform's Sentinel Agent

            RAI Pillar : Security
            Pattern    : Domestic Transfer Structuring

        The trailing newline ensures the next section (Violation(s) / bullets)
        is separated by one blank line.
        """
        lines = [f"{role_label} {action} {entity_label}", ""]
        if ctx.rai_pillar:
            lines.append(f"RAI Pillar : {ctx.rai_pillar}")
        if ctx.behavioral_pattern:
            lines.append(f"Pattern    : {ctx.behavioral_pattern}")
        lines.append("")   # blank line before violation bullets
        return "\n".join(lines)

    @staticmethod
    def _bullet_list(items: list[tuple[str, str]]) -> str:
        return "\n".join(f"• {code}: {desc}" for code, desc in items)

    @staticmethod
    def _fallback(ctx: EvaluationContext, verdict: str) -> RenderedMessage:
        entity = ENTITY_LABELS.get(ctx.raising_entity.value, _UNKNOWN_ENTITY)
        role   = ROLE_LABELS.get(ctx.role, _UNKNOWN_ROLE)
        if verdict == "COERCIVE":
            return RenderedMessage(
                coercive      = f"{role} blocked by {entity}\n\nIncident Ref: {ctx.ref_id}",
                template_type = "A",
            )
        return RenderedMessage(
            normative        = f"{role} flagged by {entity}\n\nReview Ref: {ctx.ref_id}",
            normative_action = "REVISION",
            template_type    = "A",
        )


# ── Module-level singleton ────────────────────────────────────────────────────
# Import and use this directly — no need to instantiate per-request.

_writer = MessageWriter()


def render_message(
    violations: List[PolicyViolation],
    context:    EvaluationContext,
    verdict:    str,
) -> RenderedMessage:
    """Convenience wrapper around the module singleton."""
    return _writer.render(violations, context, verdict)
