"""
Chain Executor — orchestrates layered pack evaluation.

Execution model
---------------
Layer 1 (mandatory) always runs first.
  - abort-on-first-coercive: a COERCIVE result stops the chain immediately.
    Layer 2 never executes. The partial result is returned with pack_trace.

Layer 2 (context packs) runs only if Layer 1 did not abort.
  - Each active pack runs in its declared mode (collect-all or abort-on-first-coercive).
  - Per-dimension abort=true flag is honoured even within a collect-all pack.
  - Results are collected across all active packs.

Final verdict is MRA synthesis across all collected results from both layers.

Threshold resolution
--------------------
Each dimension is evaluated against the effective ThresholdBand from its pack,
not the global PolicyRegistry. Trust signals (session_trust_score,
app_trust_score) are used ONLY for threshold step adjustments on already-active
dimensions — they never influence pack selection (that is context_classifier's job).

Pack trace
----------
Every evaluation produces a pack_trace list recording:
  - which packs were considered
  - which activated and why (tags matched)
  - which dimensions each pack contributed
  - what threshold steps were applied
  - per-pack verdict

The pack_trace is written to the audit record and returned in the API response
so every decision is fully reproducible.

Backward compatibility
----------------------
The ChainExecutor is injected optionally. When not configured, the
PolicyOrchestrator falls back to its existing flat evaluation behaviour.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from engine.pack_loader        import Pack, PackDimension, PackLoader, ThresholdBand
from engine.context_classifier import ContextClassifier
from engine.app_stack_manager  import AppStackManager
from engine.mra                import EvalInput  # used in execute() gs_results type annotation
from core.policy_loader        import PolicyRegistry
from core.message_context      import (
    EvaluationContext, NormativeAction, PolicyViolation, RaisingEntity,
)
from core.message_writer       import render_message
from schemas.response          import (
    PolicyEvaluateResponse, Verdict,
    FailedEvaluation, PolicyResult,
)

logger = logging.getLogger(__name__)

# Verdict rank — used for abort-on-first and final worst-verdict selection
_RANK = {"PERMISSIVE": 0, "NORMATIVE": 1, "COERCIVE": 2}


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

# Evaluators scored locally by the chain executor (not requested from GS).
# These are injected into gs_by_dim as synthetic EvalInputs before pack execution.
_LOCAL_DIMS: frozenset = frozenset({"session_risk_scorer"})


def _trust_score_to_signal(trust: float) -> int:
    """
    Map session trust score (1.0 → 0.20 floor) to a 1-9 GS-compatible score.

    With the 'strict' threshold profile (permissive=[1,2], normative=[3,5],
    coercive=[6,9]) this produces:

      trust >= 0.80  →  score 1  →  PERMISSIVE   (0-1 COERCIVE events)
      trust >= 0.60  →  score 4  →  NORMATIVE    (1-2 COERCIVE events)
      trust >= 0.40  →  score 6  →  COERCIVE     (3+ COERCIVE events)
      trust <  0.40  →  score 8  →  COERCIVE     (session at/near floor)
    """
    if trust >= 0.80:
        return 1
    if trust >= 0.60:
        return 4
    if trust >= 0.40:
        return 6
    return 8

# Operator evaluation for trust-signal conditions
_OPS = {
    "lt":  lambda a, b: float(a) < float(b),
    "lte": lambda a, b: float(a) <= float(b),
    "gt":  lambda a, b: float(a) > float(b),
    "gte": lambda a, b: float(a) >= float(b),
    "eq":  lambda a, b: a == b,
}


# ── Result types ───────────────────────────────────────────────────────────────

@dataclass
class DimResult:
    """Evaluation result for a single dimension within a pack execution."""
    dimension:  str
    score:      Optional[int]
    provider:   str
    verdict:    str                   # PERMISSIVE | NORMATIVE | COERCIVE
    pack_id:    str
    step_applied: int                 # threshold step that was in effect
    effective_band: ThresholdBand


@dataclass
class PackTrace:
    """Execution trace for one pack — written to every audit record."""
    pack_id:          str
    layer:            str
    version:          str
    activated:        bool
    activation_tags:  List[str]       # which tags caused activation
    skipped_reason:   str             # why pack was not activated (if applicable)
    dimensions_evaluated: List[str]
    steps_applied:    Dict[str, int]  # dimension → step applied
    pack_verdict:     str             # worst verdict within this pack
    aborted:          bool            # True if this pack triggered an abort


@dataclass
class ChainResult:
    """Full result from a chain execution."""
    response:   PolicyEvaluateResponse
    pack_traces: List[PackTrace]
    hitl:       bool                  # True if any active pack has hitl_trigger=True
                                      # and the verdict is non-PERMISSIVE
    hitl_packs: List[str]             # IDs of packs that triggered HITL


# ── Threshold Resolver (inner utility) ────────────────────────────────────────

class _ThresholdResolver:
    """
    Computes the effective ThresholdBand for a dimension within a pack
    given the current trust signals.

    Step computation:
      1. Start at step=0 (base profile).
      2. For each context_adjustment in the pack whose condition fires against
         the trust signals, add its step delta to the running total for each
         affected dimension.
      3. Clamp total step to [profile.min_step, profile.max_step].
      4. Look up the ThresholdBand from the profile's step_map.
    """

    def __init__(self, pack_loader: PackLoader):
        self._loader = pack_loader

    def resolve(
        self,
        pack:               Pack,
        dim:                PackDimension,
        trust_signals:      Dict[str, Optional[float]],
    ) -> Tuple[ThresholdBand, int]:
        """
        Returns (effective_band, step_applied) for the given dimension.
        """
        profile = self._loader.get_profile(dim.profile)
        if profile is None:
            logger.warning(
                f"ThresholdResolver: profile '{dim.profile}' not found "
                f"for dim '{dim.id}' in pack '{pack.id}' — using standard fallback"
            )
            # Return a safe default so evaluation can continue
            from engine.pack_loader import ThresholdBand
            fallback = ThresholdBand(
                permissive=(1, 3), normative=(4, 6), coercive=(7, 9)
            )
            return fallback, 0

        # Accumulate step deltas from all matching context adjustments
        total_step = 0
        for adj in pack.context_adjustments:
            # Skip adjustments that don't affect this dimension
            if adj.affects and dim.id not in adj.affects:
                continue

            signal_value = trust_signals.get(adj.signal)
            if signal_value is None:
                # Signal not available yet (e.g. app_trust_score before COGS)
                continue

            op_fn = _OPS.get(adj.operator)
            if op_fn and op_fn(signal_value, adj.value):
                total_step += adj.step
                logger.debug(
                    f"ThresholdResolver: step +{adj.step} applied to '{dim.id}' "
                    f"in pack '{pack.id}' "
                    f"({adj.signal}={signal_value} {adj.operator} {adj.value})"
                )

        effective_band = profile.resolve(total_step)
        # profile.resolve clamps the step — log the clamped effective step
        clamped = max(profile.min_step, min(profile.max_step, total_step))

        return effective_band, clamped


# ── Chain Executor ─────────────────────────────────────────────────────────────

class ChainExecutor:
    """
    Executes the two-layer policy chain deterministically.

    Dependencies are injected at construction time.
    All runtime methods are read-only with respect to configuration state.
    """

    def __init__(
        self,
        pack_loader:       PackLoader,
        classifier:        ContextClassifier,
        policy_registry:   PolicyRegistry,      # fallback for dims not in any pack
        app_stack_manager: Optional[AppStackManager] = None,
    ):
        self._loader   = pack_loader
        self._clf      = classifier
        self._registry = policy_registry
        self._resolver = _ThresholdResolver(pack_loader)
        self._app_stacks = app_stack_manager    # None = no app-stack support

    # ── Public entry point ─────────────────────────────────────────────────────

    def execute(
        self,
        request_id:    str,
        role:          str,
        metadata:      Optional[Dict[str, Any]],
        gs_results:    List[EvalInput],    # already fetched from Guardrail Service
        deferred_dims: Set[str],
        session_trust_score: Optional[float] = None,
        app_trust_score:     Optional[float] = None,
        app_id:              Optional[str]   = None,
        session_id:          Optional[str]   = None,
    ) -> ChainResult:
        """
        Run the full two-layer chain and return a ChainResult.

        gs_results  — flat list of EvalInput objects from the Guardrail Service.
                      Keyed by dimension name for lookup during pack execution.
        """
        trust_signals = {
            "session_trust_score": session_trust_score,
            "app_trust_score":     app_trust_score,
        }

        # Index GS results by dimension for O(1) lookup
        gs_by_dim: Dict[str, EvalInput] = {e.dimension: e for e in gs_results}

        # Inject locally-computed evaluators (not from GS).
        # session_risk_scorer converts the session trust score to a 1-9 risk score
        # so the session_guard pack can evaluate it without a GS round-trip.
        if session_trust_score is not None:
            synthetic_score = _trust_score_to_signal(session_trust_score)
            gs_by_dim["session_risk_scorer"] = EvalInput(
                dimension="session_risk_scorer",
                score=synthetic_score,
                status="PASS" if synthetic_score <= 2 else (
                    "WARNING" if synthetic_score <= 5 else "FLAGGED"
                ),
                provider="session_signal",
            )
            logger.debug(
                f"ChainExecutor | session_risk_scorer injected "
                f"trust={session_trust_score:.3f} → score={synthetic_score}"
            )

        # ── Context classification ─────────────────────────────────────────────
        # The classifier YAML uses paths like "metadata.transaction_context.amount"
        # — i.e. it expects a wrapper dict with a "metadata" key, not the raw
        # metadata dict itself.  Wrap it here so paths resolve correctly.
        classify_ctx = {
            "metadata": metadata or {},
            "role":     role,
        }
        tags = self._clf.classify(classify_ctx)

        logger.info(
            f"ChainExecutor | role={role} "
            f"tags={sorted(tags)}"
        )

        pack_traces:    List[PackTrace]  = []
        all_dim_results: List[DimResult] = []
        hitl       = False
        hitl_packs: List[str]            = []

        # ── Layer 1: Mandatory packs ───────────────────────────────────────────
        for pack in self._loader.mandatory_packs():
            if not pack.active:
                logger.info(f"ChainExecutor: mandatory pack '{pack.id}' is disabled — skipping")
                continue
            result, trace = self._execute_pack(
                pack, role, gs_by_dim, deferred_dims, trust_signals,
                activation_tags=["*mandatory*"],  # always active — no tag needed
            )
            pack_traces.append(trace)
            all_dim_results.extend(result)
            if pack.hitl_trigger and any(r.verdict != "PERMISSIVE" for r in result):
                hitl = True
                hitl_packs.append(pack.id)
            # Per-policy HITL (migration 0019): a single policy can require human review
            # on its own non-PERMISSIVE verdict, independent of any pack's hitl_trigger.
            for _r in result:
                if _r.verdict != "PERMISSIVE":
                    _pol = self._registry.get(_r.dimension, app_id)
                    if _pol and getattr(_pol, "hitl_trigger", False):
                        hitl = True
                        _tag = f"policy:{_r.dimension}"
                        if _tag not in hitl_packs:
                            hitl_packs.append(_tag)

            # Abort if mandatory pack produced COERCIVE
            if trace.aborted or any(r.verdict == "COERCIVE" for r in result):
                logger.warning(
                    f"ChainExecutor: COERCIVE in mandatory pack '{pack.id}' "
                    f"— aborting chain, Layer 2 will not execute"
                )
                # Mark all Layer 2 packs as skipped due to layer_1_abort
                for p2 in self._loader.layer2_packs():
                    pack_traces.append(PackTrace(
                        pack_id=p2.id,
                        layer=p2.layer,
                        version=p2.version,
                        activated=False,
                        activation_tags=[],
                        skipped_reason="layer_1_abort",
                        dimensions_evaluated=[],
                        steps_applied={},
                        pack_verdict="SKIPPED",
                        aborted=False,
                    ))
                response = self._synthesise(
                    request_id, all_dim_results, deferred_dims,
                    app_id=app_id, session_id=session_id, role=role,
                )
                return ChainResult(response=response, pack_traces=pack_traces, hitl=hitl, hitl_packs=hitl_packs)

        # ── App stack filtering ────────────────────────────────────────────────
        # If the app has an explicit stack entry, its assigned+active packs ARE
        # the effective Layer 2 pack list — no context-tag intersection required.
        # The explicit assignment by an admin is itself the activation decision.
        #
        # If the app has NO stack entry, fall back to context-tag routing
        # (backward-compatible default — used before onboarding).
        app_stack = (
            self._app_stacks.get_stack(app_id)
            if (self._app_stacks and app_id and self._app_stacks.has_stack(app_id))
            else None
        )
        if app_stack:
            # Explicit stack → assignments are the source of truth for Layer 2
            effective_pack_ids = [
                pid for pid, a in app_stack.assignments.items() if a.active
            ]
            logger.info(
                f"ChainExecutor | explicit app_stack for '{app_id}': "
                f"effective_packs={sorted(effective_pack_ids)} tags={sorted(tags)}"
            )
        else:
            # No stack entry → unregistered app: only mandatory pack runs.
            # Domain packs are never activated speculatively for unregistered apps —
            # the app_stack is the sole authoritative source for pack activation.
            effective_pack_ids = []
            logger.info(
                f"ChainExecutor | no app_stack for '{app_id}': "
                f"unregistered — mandatory pack only, no domain packs activated"
            )

        # ── Layer 2: Context packs ─────────────────────────────────────────────
        for pack_id in effective_pack_ids:
            pack = self._loader.get_pack(pack_id)
            if pack is None or pack.is_mandatory:
                continue   # mandatory already ran above
            if not pack.active:
                logger.info(f"ChainExecutor: pack '{pack_id}' is disabled — skipping")
                continue

            # Collect app-level dimension overrides for this pack (if any)
            dim_overrides = {}
            if app_stack and pack_id in app_stack.assignments:
                for ov in app_stack.assignments[pack_id].overrides:
                    dim_overrides[ov.dimension_id] = ov

            result, trace = self._execute_pack(
                pack, role, gs_by_dim, deferred_dims, trust_signals,
                activation_tags=sorted(tags),
                dim_overrides=dim_overrides,
            )
            pack_traces.append(trace)
            all_dim_results.extend(result)
            if pack.hitl_trigger and any(r.verdict != "PERMISSIVE" for r in result):
                hitl = True
                hitl_packs.append(pack.id)
            # Per-policy HITL (migration 0019): a single policy can require human review
            # on its own non-PERMISSIVE verdict, independent of any pack's hitl_trigger.
            for _r in result:
                if _r.verdict != "PERMISSIVE":
                    _pol = self._registry.get(_r.dimension, app_id)
                    if _pol and getattr(_pol, "hitl_trigger", False):
                        hitl = True
                        _tag = f"policy:{_r.dimension}"
                        if _tag not in hitl_packs:
                            hitl_packs.append(_tag)

        # ── Note packs that were NOT activated (observability) ─────────────────
        activated_ids = {t.pack_id for t in pack_traces}
        for pack in self._loader.layer2_packs():
            if pack.id not in activated_ids and pack.id not in effective_pack_ids:
                pack_traces.append(PackTrace(
                    pack_id=pack.id,
                    layer=pack.layer,
                    version=pack.version,
                    activated=False,
                    activation_tags=[],
                    skipped_reason="no_stack_assignment",
                    dimensions_evaluated=[],
                    steps_applied={},
                    pack_verdict="SKIPPED",
                    aborted=False,
                ))

        response = self._synthesise(
            request_id, all_dim_results, deferred_dims,
            app_id=app_id, session_id=session_id, role=role,
        )
        return ChainResult(response=response, pack_traces=pack_traces, hitl=hitl, hitl_packs=hitl_packs)

    # ── Pack execution ─────────────────────────────────────────────────────────

    def _execute_pack(
        self,
        pack:            Pack,
        role:            str,
        gs_by_dim:       Dict[str, EvalInput],
        deferred_dims:   Set[str],
        trust_signals:   Dict[str, Optional[float]],
        activation_tags: List[str],
        dim_overrides:   Optional[Dict] = None,   # dim_id → DimOverride
    ) -> Tuple[List[DimResult], PackTrace]:
        """Execute one pack and return its dim results and trace entry."""
        applicable = pack.dims_for_role(role)
        evaluated_ids: List[str]       = []
        steps_applied: Dict[str, int]  = {}
        dim_results:   List[DimResult] = []
        pack_aborted = False

        for dim in applicable:
            if dim.id in self._registry.disabled_dims:
                # Policy explicitly disabled (active=0) in the PAP. Do not evaluate it
                # at all — no score, no verdict, and no row in the evaluation trail —
                # so a disabled policy reads as "not enforced" rather than "passed".
                logger.debug(f"ChainExecutor: dim '{dim.id}' policy disabled — not evaluated")
                continue
            if dim.id in deferred_dims:
                logger.debug(f"ChainExecutor: dim '{dim.id}' deferred — skipped")
                continue

            gs_eval = gs_by_dim.get(dim.id)
            if gs_eval is None or gs_eval.score is None:
                # GS did not return a score for this dimension.
                # This can happen if the GS does not have the dimension registered
                # (e.g. new pack dimension not yet in evaluations.yaml).
                # We cannot infer a verdict — skip cleanly.
                logger.debug(
                    f"ChainExecutor: no GS score for dim '{dim.id}' "
                    f"in pack '{pack.id}' — excluded"
                )
                continue

            # Apply app-level override: swap profile/severity before resolving
            effective_dim = dim
            if dim_overrides and dim.id in dim_overrides:
                ov = dim_overrides[dim.id]
                effective_dim = PackDimension(
                    id=dim.id,
                    profile=ov.profile   if ov.profile   is not None else dim.profile,
                    severity=ov.severity if ov.severity  is not None else dim.severity,
                    abort=dim.abort,
                    roles=dim.roles,
                    note=dim.note,
                )
                logger.debug(
                    f"ChainExecutor: app override on '{dim.id}' — "
                    f"profile {dim.profile}→{effective_dim.profile}, "
                    f"severity {dim.severity}→{effective_dim.severity}"
                )

            # Resolve effective threshold for this dim given trust signals
            effective_band, step = self._resolver.resolve(pack, effective_dim, trust_signals)
            steps_applied[dim.id] = step

            verdict = effective_band.verdict_for(gs_eval.score)
            evaluated_ids.append(dim.id)

            dim_results.append(DimResult(
                dimension=dim.id,
                score=gs_eval.score,
                provider=gs_eval.provider or "unknown",
                verdict=verdict,
                pack_id=pack.id,
                step_applied=step,
                effective_band=effective_band,
            ))

            logger.debug(
                f"ChainExecutor [{pack.id}] dim={dim.id} "
                f"score={gs_eval.score} step={step} verdict={verdict}"
            )

            # Abort check: per-dimension abort flag OR abort-on-first-coercive pack mode
            if verdict == "COERCIVE" and (
                dim.abort
                or pack.mode == "abort-on-first-coercive"
            ):
                logger.warning(
                    f"ChainExecutor: abort triggered by dim '{dim.id}' "
                    f"in pack '{pack.id}' (verdict=COERCIVE)"
                )
                pack_aborted = True
                break

        # Compute pack-level verdict (worst across its dims)
        if dim_results:
            pack_verdict = max(
                (r.verdict for r in dim_results),
                key=lambda v: _RANK.get(v, 0)
            )
        else:
            pack_verdict = "PERMISSIVE"

        trace = PackTrace(
            pack_id=pack.id,
            layer=pack.layer,
            version=pack.version,
            activated=True,
            activation_tags=activation_tags,
            skipped_reason="",
            dimensions_evaluated=evaluated_ids,
            steps_applied=steps_applied,
            pack_verdict=pack_verdict,
            aborted=pack_aborted,
        )
        return dim_results, trace

    # ── Verdict synthesis ──────────────────────────────────────────────────────

    def _synthesise(
        self,
        request_id:    str,
        dim_results:   List[DimResult],
        deferred_dims: Set[str],
        app_id:        Optional[str] = None,
        session_id:    Optional[str] = None,
        role:          str           = "user_prompt",
    ) -> PolicyEvaluateResponse:
        """
        Build the final PolicyEvaluateResponse directly from pack-resolved DimResult objects.

        IMPORTANT — why we do NOT call MRA here:
        MRA Rule 4 re-evaluates thresholds via PolicyRegistry.evaluate(). For dimensions
        that live only in packs (e.g. sanctions_country_reference, eu_ai_act_*) and have
        no entry in policies.yaml, PolicyRegistry.evaluate() returns ("PERMISSIVE", None),
        which would silently discard a COERCIVE verdict the ThresholdResolver already
        computed correctly. This method uses the pre-resolved pack verdicts directly.

        PolicyRegistry is consulted only for remediation text and severity ordering —
        dimensions without a policy definition still contribute to the verdict.
        """
        if not dim_results:
            return PolicyEvaluateResponse(
                request_id=request_id,
                verdict=Verdict.PERMISSIVE,
                violations=[],
                remediations=[],
                failed_evaluations=[],
                policies=[],
            )

        # ── Rule 1: Verdict from pre-computed pack verdicts (worst case) ──────
        has_coercive  = any(r.verdict == "COERCIVE"  for r in dim_results)
        has_normative = any(r.verdict == "NORMATIVE" for r in dim_results)

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

        # ── Rules 2 & 3: Sort non-permissive, report all ──────────────────────
        non_permissive = [r for r in dim_results if r.verdict != "PERMISSIVE"]

        def _sort_key(r: DimResult):
            verdict_rank = 0 if r.verdict == "COERCIVE" else 1
            policy = self._registry.get(r.dimension)
            severity = policy.severity if policy else 2  # default mid severity
            return (verdict_rank, -severity)

        non_permissive.sort(key=_sort_key)

        ref_id = request_id[:8] if request_id else "unknown"

        violations:         List[str]               = []
        failed_evaluations: List[FailedEvaluation]  = []
        policy_results:     List[PolicyResult]      = []
        pv_list:            List[PolicyViolation]   = []

        for r in non_permissive:
            policy        = self._registry.get(r.dimension)
            policy_code   = policy.policy_code if policy else None
            readable      = r.dimension.replace("_", " ").title()
            dim_label     = f"{policy_code} · {readable}" if policy_code else readable
            suffix        = "blocked" if r.verdict == "COERCIVE" else "warning"
            violations.append(f"{dim_label}: {suffix}")

            failed_evaluations.append(FailedEvaluation(
                dimension=r.dimension,
                score=r.score,
                status="FLAGGED" if r.verdict == "COERCIVE" else "WARNING",
                provider=r.provider,
            ))

            if policy:
                policy_results.append(PolicyResult(
                    policy_name=r.dimension,
                    score=r.score,
                    verdict=Verdict[r.verdict],
                    severity=policy.severity,
                    remediation=None,   # consumers should read rendered_message
                ))

                norm_desc = policy.remediation("normative") or r.dimension
                coer_desc = policy.remediation("coercive")  or r.dimension
                if policy.policy_code:
                    pv_list.append(PolicyViolation(
                        policy_code           = policy.policy_code,
                        normative_description = norm_desc,
                        coercive_description  = coer_desc,
                        normative_action      = NormativeAction(policy.normative_action),
                        normative_guidance    = policy.normative_guidance or norm_desc,
                    ))

        # ── MessageWriter — assemble structured rendered message ──────────────
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

        return PolicyEvaluateResponse(
            request_id       = request_id,
            verdict          = final_verdict,
            rendered_message = rendered,
            violations       = violations,
            remediations     = sdk_bridge,
            failed_evaluations = failed_evaluations,
            policies         = policy_results,
        )

    # ── Utilities ──────────────────────────────────────────────────────────────

    def required_dimensions(self, role: str) -> Set[str]:
        """
        Return the union of all dimension ids needed across all packs for a given role.
        Conservative fallback — includes dims from ALL packs regardless of whether they
        will activate. Use required_dimensions_for_context() for a context-aware version
        that only requests dims from packs that will actually run.
        """
        dims: Set[str] = set()
        for pack in self._loader.mandatory_packs():
            if pack.active:
                dims.update(d.id for d in pack.dims_for_role(role))
        for pack in self._loader.layer2_packs():
            if pack.active:
                dims.update(d.id for d in pack.dims_for_role(role))
        return dims

    def required_dimensions_for_context(
        self, role: str, metadata: Optional[dict], app_id: Optional[str] = None
    ) -> Set[str]:
        """
        Context-aware version of required_dimensions.

        Runs the context classifier against the actual request metadata to
        determine which Layer 2 packs will activate, then returns only the
        dimension IDs from mandatory packs + those active Layer 2 packs.

        This means the Guardrail Service is only asked to score dimensions
        that will actually be consumed — packs that won't activate for this
        request contribute no dimensions to the GS call.  This reduces
        provider API calls and latency for requests with limited context.

        Parameters
        ----------
        role     : evaluation role (user_prompt | tool_call | …)
        metadata : raw metadata dict from the evaluation request
        """
        classify_ctx = {"metadata": metadata or {}, "role": role}
        tags         = self._clf.classify(classify_ctx)

        # Apply app stack logic — MUST mirror the same override used in execute():
        # If the app has an explicit stack entry, its active assignments are the
        # sole source of truth for Layer 2 pack selection.
        # Unregistered apps get mandatory dims only — no domain packs activated.
        if self._app_stacks and app_id and self._app_stacks.has_stack(app_id):
            app_stack = self._app_stacks.get_stack(app_id)
            effective_pack_ids = [
                pid for pid, a in app_stack.assignments.items() if a.active
            ]
            logger.debug(
                f"required_dimensions_for_context | app_stack for '{app_id}': "
                f"effective_packs={sorted(effective_pack_ids)} tags={sorted(tags)}"
            )
        else:
            # Unregistered app — no domain packs, mandatory dims only.
            effective_pack_ids = []

        dims: Set[str] = set()
        # Mandatory pack dims are always included (only active packs)
        for pack in self._loader.mandatory_packs():
            if pack.active:
                dims.update(d.id for d in pack.dims_for_role(role))
        # Layer 2: packs from effective list (stack assignments or context tags)
        for eid in effective_pack_ids:
            pack = self._loader.get_pack(eid)
            if pack and not pack.is_mandatory and pack.active:
                dims.update(d.id for d in pack.dims_for_role(role))

        # Never ask the GS to score locally-computed dimensions —
        # they are injected synthetically in execute() and have no GS provider.
        dims -= _LOCAL_DIMS

        logger.debug(
            f"required_dimensions_for_context | role={role} "
            f"tags={sorted(tags)} effective_packs={sorted(effective_pack_ids)} "
            f"dims={sorted(dims)}"
        )
        return dims

    def pack_traces_as_dicts(self, traces: List[PackTrace]) -> List[Dict]:
        """Serialise PackTrace list to plain dicts for audit record / API response."""
        result = []
        for t in traces:
            result.append({
                "pack_id":             t.pack_id,
                "layer":               t.layer,
                "version":             t.version,
                "activated":           t.activated,
                "activation_tags":     t.activation_tags,
                "skipped_reason":      t.skipped_reason,
                "dimensions_evaluated": t.dimensions_evaluated,
                "steps_applied":       t.steps_applied,
                "pack_verdict":        t.pack_verdict,
                "aborted":             t.aborted,
            })
        return result
