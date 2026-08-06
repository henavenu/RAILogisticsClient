"""
MRA — Most Restrictive Action synthesis engine.

Implements all five rules from SRS §4.5 deterministically:

  Rule 1 — Verdict Escalation
    A single COERCIVE dimension verdict produces a COERCIVE request verdict.
    NORMATIVE is returned only when >=1 dimension is normative and 0 are coercive.
    Severity plays no role here — the verdict level is determined solely by the
    most restrictive per-dimension result.

  Rule 2 — Violation Priority Ordering
    When multiple dimensions are non-permissive, their entries in violations[],
    failed_evaluations[], and remediations[] are ordered by:
      (a) verdict level — COERCIVE entries appear before NORMATIVE entries
      (b) severity descending — within the same verdict level, higher severity
          dimensions appear first.
    This determines which violation is reported as primary (index 0) so callers
    and UIs know where to focus attention. It does NOT affect the final verdict —
    that was already determined by Rule 1.
    Note: remediations are deduplicated in this order, so a higher-severity
    dimension "claims" a shared remediation text if two dimensions produce the same.

  Rule 3 — All Violations Reported
    All non-permissive dimension results appear in violations[] and
    failed_evaluations[]. Nothing is silently dropped.

  Rule 4 — Threshold Precedence
    All verdict decisions are made exclusively against published per-dimension
    policy thresholds (policies.yaml / PAP). The GS-supplied status field
    (PASS/WARNING/FLAGGED) is informational only — it reflects the GS's own
    generic band view and plays no role in PE verdict synthesis.

  Rule 5 — Deferred Dimension Exclusion
    Any dimension in the deferred set is excluded from synthesis entirely.
    It will not affect the verdict, violations, or remediations.
    Dimensions with score=None (provider error) are also excluded — a provider
    failure cannot inflate or deflate the verdict.
"""
import logging
from typing import List, Optional, Set

from schemas.response import (
    Verdict, PolicyEvaluateResponse, FailedEvaluation, PolicyResult,
)
from core.policy_loader  import PolicyRegistry
from core.message_context import (
    EvaluationContext, NormativeAction, PolicyViolation, RaisingEntity,
)
from core.message_writer import render_message

logger = logging.getLogger(__name__)


# ── Remediation template rendering ────────────────────────────────────────────

class _MissingKey(dict):
    """dict subclass that keeps unrecognised {key} placeholders intact."""
    def __missing__(self, key: str) -> str:
        return f"{{{key}}}"


def _render_remediation(template: str, ctx: dict) -> str:
    """
    Substitute {variable} tokens in a remediation template string.

    Supported tokens: {ref_id}, {app_id}, {session_id}.
    Unknown tokens are left as-is so stale templates never raise.
    """
    if not template or "{" not in template:
        return template
    try:
        return template.format_map(_MissingKey(ctx))
    except Exception:
        return template  # never break on a malformed template


class EvalInput:
    """Flattened evaluation result passed into MRA synthesis."""
    def __init__(self, dimension: str, score: Optional[int], status: str, provider: Optional[str]):
        self.dimension = dimension
        self.score     = score
        self.status    = status
        self.provider  = provider


def synthesise(
    request_id:      str,
    evaluations:     List[EvalInput],
    deferred_dims:   Set[str],
    policy_registry: PolicyRegistry,
    app_id:          Optional[str] = None,
    session_id:      Optional[str] = None,
    role:            str           = "user_prompt",
) -> PolicyEvaluateResponse:
    """
    Apply all five MRA rules and return a fully-populated PolicyEvaluateResponse.

    Args:
        request_id:      Echoed from the original request.
        evaluations:     All evaluation results from the Guardrail Service.
        deferred_dims:   Dimensions that must be excluded (Rule 5).
        policy_registry: Loaded policy definitions for threshold lookup (Rule 4).
        app_id:          Used for per-app policy lookup.
        session_id:      Included in remediation template rendering ({session_id}).
    """

    # ── Rule 5: Exclude deferred dimensions ────────────────────────────────
    active_evals = [e for e in evaluations if e.dimension not in deferred_dims]
    excluded = [e.dimension for e in evaluations if e.dimension in deferred_dims]
    if excluded:
        logger.debug(f"MRA Rule 5: excluded deferred dims: {excluded}")

    # ── Rule 4: Apply per-policy thresholds ────────────────────────────────
    # For each active evaluation, look up the policy and determine per-dim verdict.
    class _Assessed:
        def __init__(self, eval_in: EvalInput, verdict: str, policy):
            self.eval    = eval_in
            self.verdict = verdict
            self.policy  = policy

    assessed: List[_Assessed] = []
    for ev in active_evals:
        if ev.score is None:
            # Provider returned UNKNOWN — no score available.
            # The GS status label is the GS's own generic-band view, not a PE
            # policy verdict. We do not infer a score from it. Skip this dimension
            # so a provider error cannot inflate or deflate the final verdict.
            logger.warning(
                f"MRA: dimension '{ev.dimension}' has no score (status={ev.status}) "
                f"— excluded from synthesis (provider={ev.provider})"
            )
            continue
        verdict, policy = policy_registry.evaluate(ev.dimension, ev.score, app_id)
        assessed.append(_Assessed(ev, verdict, policy))

    # ── Rule 1: Verdict Escalation ─────────────────────────────────────────
    has_coercive  = any(a.verdict == "COERCIVE"  for a in assessed)
    has_normative = any(a.verdict == "NORMATIVE" for a in assessed)

    if has_coercive:
        final_verdict = Verdict.COERCIVE
    elif has_normative:
        final_verdict = Verdict.NORMATIVE
    else:
        final_verdict = Verdict.PERMISSIVE

    logger.info(f"MRA Rule 1 -> final verdict: {final_verdict.value}")

    if final_verdict == Verdict.PERMISSIVE:
        return PolicyEvaluateResponse(
            request_id=request_id,
            verdict=Verdict.PERMISSIVE,
            violations=[],
            remediations=[],
            failed_evaluations=[],
            policies=[],
        )

    # ── Rules 2 & 3: Violation priority ordering + all violations reported ────
    non_permissive = [a for a in assessed if a.verdict != "PERMISSIVE"]

    # Order violations for the response:
    #   Primary key:   verdict level (COERCIVE=0 before NORMATIVE=1)
    #   Secondary key: severity descending within the same verdict level
    # This determines index-0 of violations[] — the "headline" violation.
    # It does not affect the final verdict (already set by Rule 1 above).
    def _sort_key(a: _Assessed):
        verdict_rank = 0 if a.verdict == "COERCIVE" else 1
        severity     = a.policy.severity if a.policy else 0
        return (verdict_rank, -severity)

    non_permissive.sort(key=_sort_key)

    ref_id = request_id[:8] if request_id else "unknown"

    violations:         List[str]               = []
    failed_evaluations: List[FailedEvaluation]  = []
    policy_results:     List[PolicyResult]      = []
    pv_list:            List[PolicyViolation]   = []

    for a in non_permissive:
        # Rule 3 — all violations reported using policy code + human-readable label.
        # The label is derived from the dimension name so no extra DB lookup is needed.
        # Format: "TP-CO-001 · Sanctions Country Reference: blocked"
        policy_code   = a.policy.policy_code if a.policy else None
        readable      = a.eval.dimension.replace("_", " ").title()
        dim_label     = f"{policy_code} · {readable}" if policy_code else readable
        suffix        = "blocked" if a.verdict == "COERCIVE" else "warning"
        violations.append(f"{dim_label}: {suffix}")

        failed_evaluations.append(FailedEvaluation(
            dimension=a.eval.dimension,
            score=a.eval.score,
            status=a.eval.status,
            provider=a.eval.provider,
        ))

        if a.policy:
            policy_results.append(PolicyResult(
                policy_name=a.eval.dimension,
                score=a.eval.score,
                verdict=Verdict[a.verdict],
                severity=a.policy.severity,
                remediation=None,   # consumers should read rendered_message instead
            ))

            # Build PolicyViolation for MessageWriter — only when policy has content
            norm_desc = a.policy.remediation("normative") or a.eval.dimension
            coer_desc = a.policy.remediation("coercive")  or a.eval.dimension
            if a.policy.policy_code:
                pv_list.append(PolicyViolation(
                    policy_code           = a.policy.policy_code,
                    normative_description = norm_desc,
                    coercive_description  = coer_desc,
                    normative_action      = NormativeAction(a.policy.normative_action),
                    normative_guidance    = a.policy.normative_guidance or norm_desc,
                ))

    # ── MessageWriter — assemble structured rendered message ──────────────────
    ctx = EvaluationContext(
        role           = role,
        ref_id         = ref_id,
        raising_entity = RaisingEntity.TRUST_PLATFORM,
    )
    rendered = render_message(pv_list, ctx, final_verdict.value) if pv_list else None

    # SDK bridge — keep remediations[0] populated for GovernanceResult consumers
    sdk_bridge: List[str] = []
    if rendered:
        bridge_text = rendered.coercive or rendered.normative
        if bridge_text:
            sdk_bridge = [bridge_text]

    logger.info(
        f"MRA complete: verdict={final_verdict.value} "
        f"violations={len(violations)} rendered={'yes' if rendered else 'no'}"
    )

    return PolicyEvaluateResponse(
        request_id       = request_id,
        verdict          = final_verdict,
        rendered_message = rendered,
        violations       = violations,
        remediations     = sdk_bridge,
        failed_evaluations = failed_evaluations,
        policies         = policy_results,
    )
