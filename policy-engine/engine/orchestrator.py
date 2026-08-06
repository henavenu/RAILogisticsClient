"""
Policy Orchestrator — coordinates the full evaluation pipeline.

FAIL_MODE (SRS NFR-03)
----------------------
Controls the verdict returned when the Guardrail Service is unreachable:

  FAIL_MODE=open   → PERMISSIVE  (execution proceeds unguarded)
                     Use for: dev/test, non-critical internal tools

  FAIL_MODE=closed → COERCIVE    (execution blocked)
                     Use for: production, customer-facing applications

The mode is set at startup and logged prominently.  It cannot be changed
at runtime without a restart — this is intentional.

Multi-turn session correlation (SRS §4.5.5) — Phase 4
------------------------------------------------------
When a request carries a session_id, the orchestrator:

  1. Loads (or creates) the session from the SessionStore.
  2. Runs the normal per-turn MRA → turn_verdict.
  3. Merges this turn's per-dimension scores into the session's running
     worst-score table.
  4. Re-runs MRA over the session's accumulated worst scores → session_verdict.
  5. Returns max(turn_verdict, session_verdict) as the final verdict.
  6. If the session caused escalation, a synthetic violation entry is added
     so callers can distinguish a per-turn block from a session-level block.

Requests without a session_id are evaluated statelessly (Phase 1–3 behaviour).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
import httpx
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

# ── Emergent-risk accumulator config (Phase 1) ────────────────────────────────
# Phase 1 is OBSERVATIONAL by default: the PE records the standing structuring
# verdict but does not alter the eval outcome (so existing showcases are
# unchanged). Set PE_STRUCTURING_ENFORCE=true to also emit a HITL directive on a
# CONFIRMED deterministic structuring pattern. The circuit-breaker freeze ALWAYS
# blocks (it is only set by tests / the async fleet, never by existing flows).
_STRUCTURING_ENFORCE: bool = os.getenv("PE_STRUCTURING_ENFORCE", "false").strip().lower() in ("1", "true", "yes")
_PAY_KEYWORDS = ("transfer", "wire", "send", "remit", "payment", "pay ")

# ── Conflict routing toggle (Phase 2c) ────────────────────────────────────────
# Default TRUE: the SYNC Sentinel deadlock path owns conflict/arbitration (the demo
# default, unchanged). Set PE_A2A_SYNC_SENTINEL=false to route conflict to the ASYNC
# detector loop instead: the PE then accumulates the council transcript and flags
# `conflict` WATCH so the loop adjudicates and raises HITL via the directive queue.
# "Build both, enable one or the other."
_A2A_SYNC_SENTINEL: bool = os.getenv("PE_A2A_SYNC_SENTINEL", "true").strip().lower() in ("1", "true", "yes")

# Visualization-only app scope(s): the platform Agent Activity lens emits agent_to_agent
# events under app_id="trust-platform" purely to draw the governance-plane graph. Those
# must NEVER drive real governance logic (emergent accumulators, deadlock precheck), or a
# viz event can spuriously trip the breaker / write a precheck audit into the viz scope.
_VIZ_APPS: set = {a.strip() for a in os.getenv("PE_VIZ_APPS", "trust-platform").split(",") if a.strip()}

# ── Sync fast-track adjudication for the WATCH case (Phase 3) ──────────────────
# A deliberately over-sensitive gate is only acceptable if the agent can UPHOLD or
# DECLINE the suspicion fast enough to do it in-line (Part IV §C; Phase-0 latency:
# ~169 ms p50 / ~461 ms p95 warm). On a deterministic WATCH, if enabled, the PE makes
# ONE time-boxed agentic call (Sentinel /internal/detect-sync) and acts on the result:
#   CONFIRMED → raise HITL now (uphold);  CLEARED → release (decline);
#   no answer within budget → TIER-STRICT cold-start fallback (per class).
# OFF by default so existing showcases are unchanged.
_SYNC_FASTTRACK:      bool  = os.getenv("PE_SYNC_FASTTRACK", "false").strip().lower() in ("1", "true", "yes")
_FASTTRACK_BUDGET_MS: float = float(os.getenv("PE_FASTTRACK_BUDGET_MS", "800"))

def _parse_fasttrack_tiers() -> dict:
    """Per-class cold-start fallback strictness: 'strict' fails closed (HITL on timeout),
    'lenient' fails open (allow; the async loop catches it later). Env override e.g.
    PE_FASTTRACK_TIERS='structuring:strict,collusion:strict,conflict:lenient'."""
    tiers = {"structuring": "strict", "collusion": "strict", "conflict": "lenient"}
    raw = os.getenv("PE_FASTTRACK_TIERS", "")
    for part in raw.split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            if k.strip():
                tiers[k.strip()] = v.strip().lower()
    return tiers

_FASTTRACK_TIERS: dict = _parse_fasttrack_tiers()


def _extract_payment(role: str, content: str, metadata: dict) -> Optional[Tuple[float, str]]:
    """
    Best-effort (amount, recipient) extraction for the structuring accumulator.
    Prefers structured metadata.amount; falls back to a payment-keyword-gated
    regex over the free-text content. Returns None when this is not a payment.
    """
    md = metadata or {}
    amt = md.get("amount")
    if isinstance(amt, (int, float)) and amt > 0:
        rcpt = str(md.get("recipient_id") or md.get("recipient") or md.get("payee") or "")
        return float(amt), rcpt[:40]
    if role not in ("user_prompt", "tool_call"):
        return None
    low = (content or "").lower()
    if not any(k in low for k in _PAY_KEYWORDS):
        return None
    m = re.search(r"\$?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", content)
    if not m:
        return None
    try:
        amount = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if amount <= 0:
        return None
    rm = re.search(r"\bto\s+([A-Za-z0-9 .&'-]{2,40}?)(?:\s+(?:via|using|account|acct|routing|iban)\b|[.,;]|$)",
                   content, re.I)
    rcpt = (rm.group(1).strip() if rm else "")[:40]
    return amount, rcpt

from schemas.request    import PolicyEvaluateRequest
from schemas.response   import (
    PolicyEvaluateResponse, SessionSummary, Verdict,
    Directive, DirectiveAction, HITLCategory, DirectiveSource,
)
from engine.mra         import synthesise, EvalInput
from engine.chain_executor import ChainExecutor
from engine.app_trust_tracker import AppTrustTracker
from engine.session_attack_detector import SessionAttackDetector
from core.policy_loader import PolicyRegistry
from audit.writer                    import AuditWriter, AuditRecord
from audit.hitl_writer               import HitlAlertWriter
from audit.governance_alerts_writer  import GovernanceAlertsWriter
from audit.sentinel_webhook          import SentinelWebhookForwarder
from audit.sentinel_sync_client      import SentinelSyncClient
from session.store      import SessionStore
from session.dedup      import check_and_record as _dedup_check
from session.store      import SESSION_DEDUP_THRESHOLD, SESSION_DEDUP_WINDOW
from session            import blocklist as _blocklist

logger = logging.getLogger(__name__)

# Canonical fail-mode values
FAIL_OPEN   = "open"
FAIL_CLOSED = "closed"

# Verdict rank for comparison (higher = more restrictive)
_VERDICT_RANK = {
    Verdict.PERMISSIVE: 0,
    Verdict.NORMATIVE:  1,
    Verdict.COERCIVE:   2,
}


def _compute_composite(dimension_scores: dict) -> float:
    """
    Compute a session composite trust score from accumulated worst-case scores.

    Each dimension score (1-9) is normalised to a safety value:
      1.0 = perfectly clean   (raw score = 1)
      0.0 = maximum risk      (raw score = 9)

    The composite is the unweighted mean across all tracked dimensions.
    A future COGS layer can apply severity weights per dimension.
    """
    if not dimension_scores:
        return 1.0
    normalised = [1.0 - (score - 1) / 8.0 for score in dimension_scores.values()]
    return round(sum(normalised) / len(normalised), 3)


class PolicyOrchestrator:
    """
    Coordinates evaluation: guardrail dispatch → MRA synthesis → session
    accumulation → audit write → response.

    All dependencies are injected so they can be independently swapped
    (mock clients in tests, real services in production).
    """

    def __init__(
        self,
        guardrail_url:            str,
        policy_registry:          PolicyRegistry,
        deferred_dims:            set,
        http_timeout:             float = 10.0,
        fail_mode:                str   = FAIL_OPEN,
        audit_writer:             Optional[AuditWriter]         = None,
        session_store:            Optional[SessionStore]        = None,
        chain_executor:           Optional[ChainExecutor]       = None,
        app_trust_tracker:        Optional[AppTrustTracker]     = None,
        hitl_alert_writer:             Optional[HitlAlertWriter]          = None,
        governance_alerts_writer:      Optional[GovernanceAlertsWriter]  = None,
        session_attack_detector:       Optional[SessionAttackDetector]    = None,
        sentinel_webhook_forwarder:    Optional[SentinelWebhookForwarder] = None,
        sentinel_trigger_n:            int                                 = 5,
        sentinel_sync_client:          Optional[SentinelSyncClient]       = None,
        a2a_message_cap:               int                                 = 5,
    ):
        self._guardrail_url              = guardrail_url.rstrip("/")
        self._policy_registry            = policy_registry
        self._deferred_dims              = deferred_dims
        self._http_timeout               = http_timeout
        self._fail_mode                  = fail_mode.lower().strip() if fail_mode else FAIL_OPEN
        self._audit_writer               = audit_writer
        self._session_store              = session_store
        self._chain_executor             = chain_executor
        self._app_trust_tracker          = app_trust_tracker
        self._hitl_alert_writer          = hitl_alert_writer
        self._governance_alerts_writer   = governance_alerts_writer
        self._session_attack_detector    = session_attack_detector
        self._sentinel_webhook_forwarder = sentinel_webhook_forwarder
        self._sentinel_trigger_n         = max(1, int(sentinel_trigger_n))
        # Global evaluation counter — increments on every PE evaluation regardless
        # of session.  Sentinel fires when counter % N == 0, so it triggers within
        # N requests of PE startup and naturally pulls all historical audit events.
        self._sentinel_eval_counter      = 0
        # ── Sentinel sync (inline conflict/deadlock detection) ─────────────────
        self._sentinel_sync_client       = sentinel_sync_client
        self._a2a_message_cap            = max(1, int(a2a_message_cap))

        # ── Session-scoped directive store ─────────────────────────────────────
        # Directives injected by Sentinel (via POST /v1/session/{id}/directive)
        # are queued here and attached to the next evaluate() response for that
        # session.  Thread-safe: the lock protects both reads and writes.
        # Upgrade to Redis-backed store for multi-process / HA deployments.
        self._pending_directives: Dict[str, List[Directive]] = {}
        self._directives_lock = threading.Lock()

        if chain_executor:
            logger.info("PolicyOrchestrator: ChainExecutor active — pack-aware evaluation enabled")
        else:
            logger.info("PolicyOrchestrator: ChainExecutor not configured — flat evaluation mode")
        if session_attack_detector:
            logger.info("PolicyOrchestrator: SessionAttackDetector active — multi-turn sliding attack detection enabled")
        else:
            logger.info("PolicyOrchestrator: SessionAttackDetector not configured — per-turn detection only")
        if sentinel_webhook_forwarder:
            logger.info(
                f"PolicyOrchestrator: Sentinel webhook active — "
                f"fires on COERCIVE verdict OR every {self._sentinel_trigger_n} evaluations "
                f"(global counter, resets on restart)"
            )
        else:
            logger.info("PolicyOrchestrator: Sentinel webhook not configured")
        if sentinel_sync_client:
            logger.info(
                f"PolicyOrchestrator: Sentinel sync client active — "
                f"A2A cap={self._a2a_message_cap} messages ({self._a2a_message_cap // 2} full cycles)"
            )
        else:
            logger.info("PolicyOrchestrator: Sentinel sync client not configured — A2A conflict detection disabled")
        logger.info(
            "PolicyOrchestrator: session risk guard via session_guard pack "
            "(Tier 0 removed — high-risk enforcement now driven by policy severity)"
        )

        if self._fail_mode not in (FAIL_OPEN, FAIL_CLOSED):
            logger.warning(
                f"Unknown FAIL_MODE '{self._fail_mode}' — defaulting to '{FAIL_OPEN}'. "
                f"Valid values: open | closed"
            )
            self._fail_mode = FAIL_OPEN

    # ── Public ─────────────────────────────────────────────────────────────────

    @property
    def fail_mode(self) -> str:
        return self._fail_mode

    def receive_directive(self, session_id: str, directive: Directive) -> None:
        """
        Queue a directive for a session.

        Called by the Sentinel→PE callback endpoint
        (POST /v1/session/{session_id}/directive) when the Sentinel detects a
        cross-turn pattern (e.g. agent_circular_dispute threshold exceeded).
        The directive is attached to the next evaluate() response for the session.

        Thread-safe.  Never raises — errors are logged and swallowed.
        """
        if not session_id:
            logger.warning("[orchestrator] receive_directive called with empty session_id — ignored")
            return
        with self._directives_lock:
            self._pending_directives.setdefault(session_id, []).append(directive)
        logger.info(
            f"[orchestrator] Directive queued — session={session_id} "
            f"action={directive.action.value} category={directive.category} "
            f"source={directive.source.value}"
        )

    # ── Emergent-risk circuit-breaker / risk-record API (Phase 1) ─────────────
    def set_session_freeze(self, session_id: str, app_id: str, frozen: bool,
                           reason: str = "", ttl: float = 0.0) -> Optional[dict]:
        """Trip/clear the per-session circuit-breaker freeze. Called by tests now and
        by the async detector fleet (Phase 2). The gate enforces it on every eval."""
        if self._session_store is None:
            return None
        st = self._session_store.set_freeze(session_id, app_id, frozen, reason, ttl)
        if st is None:
            return None
        logger.warning("[breaker] session=%s freeze=%s reason=%r", session_id, st.is_frozen(), st.freeze_reason)
        return {"session_id": session_id, "app_id": app_id,
                "frozen": st.is_frozen(), "freeze_reason": st.freeze_reason}

    def set_session_verdict(self, session_id: str, app_id: str,
                            verdict: str, reason: str = "",
                            risk_class: str = "") -> Optional[dict]:
        """Record the standing emergent-risk verdict (set by the async detector fleet).
        With risk_class, updates only that class and recomputes the worst-of rollup."""
        if self._session_store is None:
            return None
        st = self._session_store.set_risk_verdict(session_id, app_id, verdict, reason,
                                                  risk_class=risk_class)
        if st is None:
            return None
        logger.info("[emergent-risk] session=%s class=%s verdict<-%s (agent): %s",
                    session_id, risk_class or "rollup", verdict, reason)
        return {"session_id": session_id, "app_id": app_id,
                "risk_verdict": st.risk_verdict, "risk_reason": st.risk_reason,
                "risk_flags": dict(st.risk_flags)}

    def get_session_risk(self, session_id: str, app_id: str) -> Optional[dict]:
        """Read the standing per-session risk record (verdict, freeze, accumulators)."""
        if self._session_store is None:
            return None
        st = self._session_store.get(session_id, app_id)
        if st is None:
            return None
        _sv, _sr = st.assess_structuring()
        return {
            "session_id": session_id, "app_id": app_id,
            "risk_verdict": st.risk_verdict, "risk_reason": st.risk_reason,
            "risk_flags": dict(st.risk_flags),
            "frozen": st.is_frozen(), "freeze_reason": st.freeze_reason,
            "payment_count": len(st.payment_events),
            # Compact payment stream for the async detector (the agent's-eyes input).
            "payments": [{"amount": p.get("amount"), "recipient": p.get("recipient"),
                          "ts": p.get("ts")} for p in st.payment_events][-25:],
            "structuring_now": {"verdict": _sv, "reason": _sr},
            "a2a_tool_counts": dict(st.a2a_tool_counts),
        }

    def get_flagged_sessions(self, app_id: str) -> List[dict]:
        """
        Risk-store-driven discovery for the async emergent detector (Phase 2).

        Returns the compact risk record (verdict, reason, payment stream) for every
        live session the deterministic gate has flagged WATCH/CONFIRMED and not yet
        frozen. Replaces audit-firehose discovery, which misses /v1/app/evaluate
        sessions — the detector now sees every flagged session regardless of path.
        """
        if self._session_store is None:
            return []
        out: List[dict] = []
        for st in self._session_store.list_flagged_sessions(app_id):
            out.append({
                "session_id":   st.session_id,
                "app_id":       st.app_id,
                "risk_verdict": st.risk_verdict,
                "risk_reason":  st.risk_reason,
                "risk_flags":   dict(st.risk_flags),
                "frozen":       st.is_frozen(),
                "payments": [{"amount": p.get("amount"), "recipient": p.get("recipient"),
                              "ts": p.get("ts")} for p in st.payment_events][-25:],
                # Collusion firehose (A2A message contents) for the agentic detector.
                "a2a": [str(e.get("content", "")) for e in st.collusion_events][-40:],
                # Council deliberation transcript for the async conflict detector.
                "conflict_a2a": [str(e.get("content", "")) for e in st.conflict_events][-40:],
            })
        return out

    # ── Phase 3: sync fast-track adjudication for the WATCH case ───────────────
    def _fast_track_watch(self, risk_class: str, stream: list) -> Tuple[str, str, float]:
        """
        Time-boxed inline agentic uphold/decline for a deterministic WATCH.
        Returns (outcome, reason, latency_ms) where outcome ∈
          UPHOLD         — agent CONFIRMED → escalate now
          DECLINE        — agent CLEARED → release the false positive
          FALLBACK_HITL  — no answer in budget AND class tier is 'strict' (fail closed)
          FALLBACK_ALLOW — no answer in budget AND class tier is 'lenient' (fail open;
                           the async loop will still catch a real pattern later)
        """
        tier = _FASTTRACK_TIERS.get(risk_class, "lenient")
        _fallback = "FALLBACK_HITL" if tier == "strict" else "FALLBACK_ALLOW"
        if self._sentinel_sync_client is None:
            return _fallback, "no_sentinel_client", 0.0
        res = self._sentinel_sync_client.detect_sync(risk_class, stream, _FASTTRACK_BUDGET_MS)
        if res is None:
            return _fallback, "timeout_or_error", _FASTTRACK_BUDGET_MS
        det = str(res.get("determination", "?")).upper()
        lat = float(res.get("latency_ms", 0.0) or 0.0)
        if det == "CONFIRMED":
            return "UPHOLD", str(res.get("reason", "")), lat
        if det == "CLEARED":
            return "DECLINE", str(res.get("reason", "")), lat
        return _fallback, "indeterminate", lat   # '?' parse failure → tier fallback

    def _build_fasttrack_directive(self, risk_class: str, reason: str, fallback: bool) -> Directive:
        """HITL directive raised when the fast-track upholds (or fails closed) a WATCH."""
        category = (HITLCategory.SANCTIONS_REVIEW if risk_class == "structuring"
                    else HITLCategory.AGENT_ARBITRATION)
        note = ("fast-track unavailable — tier-strict fail-closed" if fallback
                else "fast-track agent upheld the suspicion")
        return Directive(
            action=DirectiveAction.REQUIRE_HUMAN_REVIEW,
            category=category,
            reason=f"[{risk_class}] {note}: {reason}"[:240],
            source=DirectiveSource.SENTINEL,
            metadata={"fasttrack": True, "risk_class": risk_class, "fallback": fallback},
        )

    def _drain_pending_directives(self, session_id: Optional[str]) -> List[Directive]:
        """
        Remove and return all queued directives for a session.
        Returns an empty list when session_id is None or no directives are pending.
        Thread-safe.
        """
        if not session_id:
            return []
        with self._directives_lock:
            return self._pending_directives.pop(session_id, [])

    def _extract_proposed_options(self, a2a_messages: list) -> list:
        """
        Parse previous_card_ids from the last accumulated A2A message to build
        a proposed_options list for Sentinel's random-pick step.

        The _format_a2a_tool_content helper in the SDK formats tool call args as
        key: value lines, so the last message will contain a line like:
            previous_card_ids: card_001,card_002,card_003

        Returns a list of {"card_id": "..."} dicts, one per card ID.
        Returns [] when no ids are found (non-card A2A flows, first round, etc.).
        """
        if not a2a_messages:
            return []
        last_msg = a2a_messages[-1]
        for line in last_msg.split("\n"):
            if line.startswith("previous_card_ids:"):
                ids_str = line.split(":", 1)[1].strip()
                if ids_str:
                    return [
                        {"card_id": cid.strip()}
                        for cid in ids_str.split(",")
                        if cid.strip()
                    ]
        return []

    # Tools whose A2A messages are counted toward the Sentinel sync HITL threshold.
    # Only iterative negotiation/deliberation tools that can reach a deadlock or an
    # irreconcilable multi-agent conflict should be tracked. One-shot routing A2A
    # tools (e.g. international_transfer, batch_coordinate) must NOT count — they are
    # single-message delegations, not negotiation rounds, and including them would
    # fire Sentinel on the wrong round of a card advisory that happens to run after
    # a wire transfer in the same session.
    #   request_card_research  — card_profiler ↔ card_researcher negotiation (Story 4)
    #   request_policy_review  — payment-surge council deliberation (Story 5)
    _A2A_SENTINEL_TOOLS: frozenset = frozenset({
        "request_card_research", "request_policy_review",
        # Fraud Review Authority reviewers — each is its own A2A tool so it counts
        # toward the council deliberation cap (and lights up as a distinct agent).
        "transaction_monitoring_review", "revenue_review", "cx_review", "risk_review",
    })

    # Per-flow A2A cap override (additive). Tools not listed fall back to the
    # global self._a2a_message_cap (5). The Fraud Review Authority runs 2 polling
    # rounds × 4 reviewers + 1 coordinator escalation = 9 A2A messages before the
    # PE escalates to Sentinel — so the council tools use a cap of 9. card_research
    # is intentionally absent here and keeps the default cap of 5 (cards unaffected).
    _A2A_CAP_OVERRIDES: dict = {
        "transaction_monitoring_review": 9,
        "revenue_review":                9,
        "cx_review":                     9,
        "risk_review":                   9,
        "request_policy_review":         9,
    }

    def _check_a2a_sentinel(
        self,
        session_id: str,
        content:    str,
        tool_name:  str = "",
    ) -> Optional[Directive]:
        """
        Accumulate A2A message content for a session and, once the cap is reached,
        invoke Sentinel synchronously to classify the exchange as a conflict/deadlock.

        Returns
        -------
        Directive  REQUIRE_HUMAN_REVIEW with AGENT_ARBITRATION category when Sentinel
                   returns NORMATIVE — queued via receive_directive() so the directive
                   is attached to the CURRENT response (step 8c drains it immediately).
        None       When the cap has not been reached, a2a_hitl_fired is already True,
                   Sentinel returns PERMISSIVE, or any error occurs (fail-open).

        Design notes
        ------------
        • Only called for role=agent_to_agent requests — enforced by the caller (step 5b).
        • Only tool_names in _A2A_SENTINEL_TOOLS are counted toward the cap.
          One-shot routing A2A calls (international_transfer, batch_coordinate, etc.)
          are single-message delegations, not negotiation rounds.  Counting them would
          fire Sentinel at the wrong round of a card advisory that occurs after a wire
          transfer in the same session (the threshold would be reached mid-flow before
          the deadlock-signalling message is sent, causing Sentinel to return PERMISSIVE
          and permanently setting a2a_hitl_fired=True, blocking all subsequent rounds).
        • a2a_hitl_fired prevents repeated HITL triggers after the cap is first hit.
        • The full accumulated message list (up to cap) is passed to Sentinel so the LLM
          can see the complete interaction context, not just the triggering message.
        • Sync mode: no Trust Radar post, no VerdictStore write — Sentinel behaves as a
          pure evaluator.  The async observer path (SentinelWebhookForwarder) is
          completely decoupled and fires independently on its own trigger conditions.
        """
        if self._sentinel_sync_client is None or self._session_store is None:
            return None

        # Skip non-negotiation A2A tools — they don't produce deadlock patterns.
        # An empty tool_name (legacy callers) is treated as eligible.
        if tool_name and tool_name not in self._A2A_SENTINEL_TOOLS:
            return None

        state = self._session_store.get_or_create(session_id, app_id="")
        if state.a2a_hitl_fired:
            return None  # HITL already triggered for this session — skip

        # Accumulate the message content + per-tool counts ATOMICALLY (read-modify-write
        # under the store lock) so concurrent agent_to_agent evaluations cannot race.
        msg_count, tool_counts, state = self._session_store.accumulate_a2a(
            session_id, "", content, tool_name)

        # ── Repetition-based escalation (robust) ──────────────────────────────
        # A genuine deadlock is a repeated back-and-forth: two or more agents
        # RE-CAST their position (tool invoked >= 2x) without resolution — NOT a
        # raw message total, which is fragile to added handoffs. We escalate when
        # the coordinator makes its escalation call (ESCALATION_TOOL) AND >= 2
        # reviewer agents have repeated. A generous raw-count fallback guarantees
        # we never hang if an app uses a different escalation channel.
        ESCALATION_TOOL = "request_policy_review"   # the coordinator's escalation channel
        repeated_agents = sum(1 for t, c in tool_counts.items()
                              if c >= 2 and t != ESCALATION_TOOL)
        effective_cap   = self._A2A_CAP_OVERRIDES.get(tool_name, self._a2a_message_cap)
        is_escalation   = (tool_name == ESCALATION_TOOL)
        trip = (is_escalation and repeated_agents >= 2) or (msg_count >= effective_cap + 6)
        logger.debug(
            f"[orchestrator] A2A — session={session_id} count={msg_count} "
            f"repeated_agents={repeated_agents} escalation={is_escalation} trip={trip}"
        )

        if not trip:
            return None  # No deadlock signature yet

        # Deadlock signature reached — invoke Sentinel synchronously
        logger.info(
            f"[orchestrator] Deadlock signature ({repeated_agents} agents repeated, "
            f"{msg_count} messages) — invoking Sentinel sync eval for session={session_id}"
        )
        # Extract card options from the accumulated messages so Sentinel can
        # randomly pick one when it returns NORMATIVE (card advisor flow).
        # Non-card A2A flows return [] and proposed_options is omitted.
        proposed_options = self._extract_proposed_options(list(state.a2a_messages))
        logger.debug(
            f"[orchestrator] proposed_options for Sentinel: {proposed_options} "
            f"session={session_id}"
        )
        result = self._sentinel_sync_client.evaluate_sync(
            session_id       = session_id,
            a2a_messages     = list(state.a2a_messages),  # pass full history
            proposed_options = proposed_options if proposed_options else None,
        )

        if result is None:
            # Sentinel unreachable or timed out — fail-open
            logger.warning(
                f"[orchestrator] Sentinel sync returned None — "
                f"fail-open for session={session_id}"
            )
            return None

        verdict = (result.get("verdict") or "PERMISSIVE").upper()
        if verdict != "NORMATIVE":
            logger.info(
                f"[orchestrator] Sentinel sync verdict={verdict} — "
                f"no HITL directive for session={session_id}"
            )
            # Mark fired even on PERMISSIVE so subsequent A2A calls don't
            # re-invoke Sentinel after the cap is first evaluated.  Without
            # this, every A2A call beyond the threshold triggers a redundant
            # sync LLM evaluation even though the first one already cleared.
            state.a2a_hitl_fired = True
            self._session_store.put(state)
            return None

        # NORMATIVE: mark fired, build directive
        state.a2a_hitl_fired = True
        self._session_store.put(state)

        pattern   = result.get("pattern_type") or "agent_conflict"
        reasoning = result.get("reasoning")    or "Agent-to-agent conflict or deadlock detected"

        # Extract Sentinel's random card pick (card advisor flow only).
        # _random_pick_card in evaluator.py returns {"card_id": "...", ...} or None.
        recommended_option  = result.get("recommended_option") or {}
        recommended_card_id = (
            recommended_option.get("card_id", "")
            if isinstance(recommended_option, dict) else ""
        )

        logger.info(
            f"[orchestrator] Sentinel sync NORMATIVE — "
            f"pattern={pattern}  session={session_id}  "
            f"recommended_card_id={recommended_card_id or 'none'}  "
            f"reasoning={reasoning[:120]}"
        )

        # Carry the Sentinel finding on the directive so the caller can persist a
        # real Sentinel audit record (Option B — Sentinel as a captured actor).
        _sentinel_meta = {
            "sentinel_verdict":   verdict,
            "sentinel_pattern":   pattern,
            "sentinel_reasoning": reasoning,
        }
        if recommended_card_id:
            _sentinel_meta["recommended_card_id"] = recommended_card_id
        return Directive(
            action   = DirectiveAction.REQUIRE_HUMAN_REVIEW,
            category = HITLCategory.AGENT_ARBITRATION,
            reason   = f"Sentinel detected {pattern}: {reasoning}",
            source   = DirectiveSource.SENTINEL,
            metadata = _sentinel_meta,
        )

    def evaluate(self, request: PolicyEvaluateRequest) -> PolicyEvaluateResponse:
        """
        Full evaluation pipeline — single code path regardless of session/no-session.

        Returns a PolicyEvaluateResponse on every code path (success, fail-safe,
        session escalation).  Writes one AuditRecord at the end.
        """
        _t0 = time.monotonic()
        interaction_id = str(uuid.uuid4())
        _fail_reason: Optional[str] = None

        logger.info(
            f"evaluate | interaction={interaction_id[:8]} "
            f"app={request.app_id} "
            f"request_id={request.request_id} "
            f"role={request.role.value} "
            f"session={request.session_id or '-'}"
        )

        # ── 0. HITL_CHECKPOINT pass-through ───────────────────────────────────────
        # HITL governance events (initiated / decision / resolved) are NOT user
        # content — evaluating them through the Guardrail Service is meaningless.
        # We write the audit record (so the dashboard can see the event) and return
        # PERMISSIVE immediately, with any pending Sentinel directives attached.
        from schemas.request import ContentRole as _ContentRole
        if request.role == _ContentRole.HITL_CHECKPOINT:
            # Guardrail evaluation is skipped (this is governance bookkeeping, not
            # user content), but the audit row should carry the REAL governance
            # outcome so the interaction diagram reflects stored truth rather than
            # a render-time fabrication.  The SDK stamps metadata.governance_verdict
            # (e.g. "NORMATIVE" for a Sentinel-detected conflict that opened the
            # HITL).  Absent/invalid → PERMISSIVE (e.g. a resolution acknowledgement).
            _hc_meta = request.metadata or {}
            _hc_verdict = Verdict.PERMISSIVE
            _gv = str(_hc_meta.get("governance_verdict", "")).upper()
            if _gv in (Verdict.NORMATIVE.value, Verdict.COERCIVE.value, Verdict.PERMISSIVE.value):
                _hc_verdict = Verdict(_gv)
            _hc_response = PolicyEvaluateResponse(
                request_id=request.request_id,
                verdict=_hc_verdict,
            )
            # Attach pending Sentinel directives so they reach the SDK on this turn
            _hc_directives = self._drain_pending_directives(request.session_id)
            if _hc_directives:
                _hc_response = _hc_response.model_copy(
                    update={"directives": _hc_directives}
                )
            self._write_audit(
                request=request,
                response=_hc_response,
                interaction_id=interaction_id,
                latency_ms=(time.monotonic() - _t0) * 1000,
                fail_reason=None,
                turn_number=None,
                session_verdict=None,
                session_escalated=False,
                eval_inputs=[],
                pack_traces=[],
            )
            return _hc_response

        # ── 0a. Agent/tool block check (Sentinel-driven, session-scoped) ─────────
        # Fires before any guardrail work.  When Sentinel detects an ALERT pattern
        # (e.g. BSA §5324 structuring) it POSTs to /v1/sessions/agent-block which
        # adds the offending agent/tool to the per-session blocklist.  All
        # subsequent evaluate() calls for that agent/tool return COERCIVE instantly
        # without touching the Guardrail Service.  Other agents in the session
        # (account, profile) are unaffected.
        if request.session_id:
            _meta       = request.metadata or {}
            _agent_name = _meta.get("agent_name", "")
            _tool_name  = _meta.get("tool_name",  "")
            _agent_hit  = _blocklist.is_agent_blocked(request.session_id, _agent_name)
            _tool_hit   = _blocklist.is_tool_blocked(request.session_id,  _tool_name)
            if _agent_hit or _tool_hit:
                _which = _agent_name if _agent_hit else _tool_name
                _kind  = "agent" if _agent_hit else "tool"
                logger.warning(
                    f"[blocklist] COERCIVE early-return — "
                    f"{_kind}={_which!r} is blocked for session={request.session_id}"
                )
                _block_response = PolicyEvaluateResponse(
                    request_id=request.request_id,
                    verdict=Verdict.COERCIVE,
                    violations=[
                        f"sentinel_block: {_kind} '{_which}' is blocked for this session "
                        f"due to detected policy violation pattern"
                    ],
                    remediations=[
                        "This agent or tool has been suspended for the current session "
                        "by the Sentinel Agent following detection of a policy violation. "
                        "Contact your compliance officer to review and resume."
                    ],
                    failed_evaluations=[],
                    policies=[],
                )
                # ── Resolve turn_number so the dashboard can group this audit record
                # into the correct turn card.  Without this it falls to turn 0 and is
                # silently dropped from the interaction graph.
                _bl_turn_number: Optional[int] = None
                if self._session_store is not None:
                    try:
                        _bl_state = self._session_store.get(request.session_id, request.app_id)
                        if _bl_state is not None:
                            _bl_turn_number = _bl_state.turn_count
                            # user_prompt would open a new turn — account for the increment
                            # that _apply_session would have performed had we not short-circuited.
                            if request.role.value == "user_prompt":
                                _bl_turn_number += 1
                            _bl_turn_number = max(1, _bl_turn_number)
                    except Exception as _bl_exc:
                        logger.debug(
                            f"[blocklist] Could not read session turn_count "
                            f"for turn_number resolution: {_bl_exc}"
                        )
                self._write_audit(
                    request=request,
                    response=_block_response,
                    interaction_id=str(uuid.uuid4()),
                    latency_ms=0.0,
                    fail_reason="sentinel_block",
                    turn_number=_bl_turn_number,
                    session_verdict=None,
                    session_escalated=False,
                )
                return _block_response

        # ── 0b. Circuit-breaker freeze + emergent-risk accumulators (Phase 1) ────
        # The per-session risk record carries a `freeze` set by the async detector
        # fleet (Phase 2) on a CONFIRMED runaway. While frozen, EVERY action in the
        # session is blocked here — a cheap sub-ms read, fail-safe (Part III #4). We
        # also update the deterministic structuring accumulator (the gate's cheap
        # digest) and record the standing risk verdict. OBSERVATIONAL by default:
        # the verdict is recorded but does not change the outcome unless a freeze is
        # active, or PE_STRUCTURING_ENFORCE is on (off by default → showcases unchanged).
        if request.session_id and self._session_store is not None and request.app_id not in _VIZ_APPS:
            try:
                _rk = self._session_store.get(request.session_id, request.app_id)
            except Exception:
                _rk = None
            # (i) breaker enforcement — frozen session blocks immediately
            if _rk is not None and _rk.is_frozen():
                logger.warning(
                    f"[breaker] COERCIVE early-return — session={request.session_id} FROZEN: "
                    f"{_rk.freeze_reason or 'emergent-risk runaway'}"
                )
                _bk_resp = PolicyEvaluateResponse(
                    request_id=request.request_id,
                    verdict=Verdict.COERCIVE,
                    violations=[f"circuit_breaker: session frozen — {_rk.freeze_reason or 'emergent-risk runaway'}"],
                    remediations=["The session is paused by the platform circuit-breaker following a detected "
                                  "emergent-risk pattern. A reviewer must clear the freeze to resume."],
                    failed_evaluations=[],
                    policies=[],
                )
                _bk_dir = Directive(
                    action=DirectiveAction.REQUIRE_HUMAN_REVIEW,
                    category=HITLCategory.AGENT_ARBITRATION,
                    reason=f"Circuit-breaker freeze: {_rk.freeze_reason or 'emergent-risk runaway'}",
                    source=DirectiveSource.PE,
                    metadata={"breaker": True, "freeze_reason": _rk.freeze_reason},
                )
                _bk_resp = _bk_resp.model_copy(update={"directives": [_bk_dir]})
                _bk_turn = max(1, _rk.turn_count + (1 if request.role.value == "user_prompt" else 0))
                self._write_audit(
                    request=request, response=_bk_resp, interaction_id=str(uuid.uuid4()),
                    latency_ms=0.0, fail_reason="circuit_breaker", turn_number=_bk_turn,
                    session_verdict=None, session_escalated=False,
                )
                return _bk_resp
            # (ii) emergent-risk accumulator — payments → structuring digest + verdict
            _pay = _extract_payment(request.role.value, request.content or "", request.metadata or {})
            if _pay is not None:
                _amt, _rcpt = _pay
                _rk = self._session_store.accumulate_payment(request.session_id, request.app_id, _amt, _rcpt)
                _rv, _rr = _rk.assess_structuring()
                # Phase 3: sync fast-track on the FIRST WATCH — uphold/decline in-line.
                _ft_dir = None
                if (_SYNC_FASTTRACK and _rv == "WATCH"
                        and "structuring" not in _rk.fasttrack_done):
                    _stream = [{"amount": p.get("amount"), "recipient": p.get("recipient")}
                               for p in _rk.payment_events][-25:]
                    _oc, _ocr, _lat = self._fast_track_watch("structuring", _stream)
                    logger.warning("[fast-track] session=%s structuring WATCH -> %s (%.0fms): %s",
                                   request.session_id, _oc, _lat, _ocr[:80])
                    if _oc in ("UPHOLD", "FALLBACK_HITL"):
                        _rv, _rr = "CONFIRMED", (f"sync fast-track "
                                                 f"{'upheld' if _oc == 'UPHOLD' else 'fail-closed'}: {_ocr}")
                        _ft_dir = self._build_fasttrack_directive("structuring", _ocr,
                                                                  fallback=(_oc == "FALLBACK_HITL"))
                    elif _oc == "DECLINE":
                        _rv, _rr = "CLEAR", f"sync fast-track declined (agent): {_ocr}"
                    _rk.fasttrack_done.append("structuring")
                    self._session_store.put(_rk)
                if _rk.risk_flags.get("structuring") != _rv:
                    self._session_store.set_risk_verdict(request.session_id, request.app_id,
                                                         _rv, _rr, risk_class="structuring")
                if _ft_dir is not None:
                    self.receive_directive(request.session_id, _ft_dir)
                if _rv != "CLEAR":
                    logger.info(f"[emergent-risk] session={request.session_id} structuring={_rv}: {_rr}")
                    if _rv == "CONFIRMED" and _STRUCTURING_ENFORCE:
                        self.receive_directive(request.session_id, Directive(
                            action=DirectiveAction.REQUIRE_HUMAN_REVIEW,
                            category=HITLCategory.AGENT_ARBITRATION,
                            reason=f"Structuring detected: {_rr}",
                            source=DirectiveSource.PE,
                            metadata={"breaker_type": "payment_structuring"},
                        ))

        # ── 0. Prompt deduplication check (user_prompt + session required) ──────
        # Exact duplicates may be blocked before hitting the GS.
        # Near-duplicates inject a prompt_repetition score into eval_inputs later.
        _dedup_result = None
        _dedup_state  = None
        if (request.role.value == "user_prompt"
                and request.session_id
                and self._session_store is not None):
            try:
                _dedup_state = self._session_store.get_or_create(
                    request.session_id, request.app_id
                )
                _dedup_result = _dedup_check(
                    prompt=request.content,
                    hashes=_dedup_state.prompt_hashes,
                    embeddings=_dedup_state.prompt_embeddings,
                    threshold=SESSION_DEDUP_THRESHOLD,
                    window=SESSION_DEDUP_WINDOW,
                )
                # Persist updated dedup cache immediately (hashes/embeddings mutated in-place)
                self._session_store.put(_dedup_state)

                if _dedup_result.is_exact_duplicate:
                    # Exact duplicate detected — log for observability but DO NOT
                    # skip the Guardrail Service call.  A repeated prompt may carry
                    # the same sanctions / policy violations as the original and must
                    # be evaluated and blocked on every turn.  The repetition_score
                    # is injected into eval_inputs below (step 2b) regardless of
                    # whether this is an exact or near-duplicate.
                    logger.info(
                        f"[Dedup] Exact duplicate detected — proceeding to GS evaluation | "
                        f"session_id={request.session_id} "
                        f"request_id={request.request_id}"
                    )
            except Exception as exc:
                logger.warning(
                    f"[Dedup] Deduplication check failed (non-fatal): {exc} — proceeding"
                )
                _dedup_result = None

        # ── 1. Call Guardrail Service ─────────────────────────────────────────
        # When the ChainExecutor is active, request only the dimensions
        # needed by the active packs for this role.  When not configured,
        # pass None (= "evaluate all registered active dimensions in GS").
        requested_dims = None
        if self._chain_executor is not None:
            # Use context-aware method: classifies request metadata first so only
            # dimensions from packs that will actually activate are requested from
            # the Guardrail Service.  This avoids paying provider API cost for
            # dimensions whose packs won't run (e.g. sanctions dims on a balance query).
            required = self._chain_executor.required_dimensions_for_context(
                request.role.value, request.metadata, app_id=request.app_id
            )
            if required:
                requested_dims = sorted(required)

        trust_payload = {
            "request_id":   request.request_id,
            "content":      request.content,
            "content_role": request.role.value,
            "evaluations":  requested_dims,  # None = all; list = specific subset
        }

        trust_response = None
        try:
            resp = httpx.post(
                f"{self._guardrail_url}/v1/trust/evaluate",
                json=trust_payload,
                timeout=self._http_timeout,
            )
            resp.raise_for_status()
            trust_response = resp.json()

        except httpx.TimeoutException:
            logger.error(
                f"Guardrail Service timed out after {self._http_timeout}s "
                f"| request_id={request.request_id}"
            )
            _fail_reason = "guardrail_timeout"

        except httpx.HTTPStatusError as e:
            logger.error(
                f"Guardrail Service returned HTTP {e.response.status_code} "
                f"| request_id={request.request_id}"
            )
            _fail_reason = "guardrail_http_error"

        except Exception as e:
            logger.error(
                f"Guardrail Service unreachable | request_id={request.request_id}: {e}"
            )
            _fail_reason = "guardrail_unreachable"

        # ── 2. Flatten provider results ───────────────────────────────────────
        eval_inputs = []
        if _fail_reason is None:
            for pr in trust_response.get("provider_results", []):
                provider_name = pr.get("provider", "unknown")
                for ev in pr.get("evaluations", []):
                    eval_inputs.append(EvalInput(
                        dimension=ev.get("dimension", "unknown"),
                        score=ev.get("score"),
                        status=ev.get("status", "UNKNOWN"),
                        provider=provider_name,
                    ))

            # Drop dimensions whose policy is explicitly disabled (active=0) in the PAP.
            # A disabled policy is treated as not loaded: its raw guardrail score is
            # discarded here so it never reaches gs_by_dim, dimension_scores, the
            # composite, or the evaluation trail — disabled reads as "not enforced",
            # never as a PERMISSIVE "passed" row.
            _disabled = self._policy_registry.disabled_dims
            if _disabled:
                eval_inputs = [ei for ei in eval_inputs if ei.dimension not in _disabled]

            if not eval_inputs:
                logger.warning(
                    f"Guardrail Service returned no evaluation results "
                    f"| request_id={request.request_id}"
                )
                _fail_reason = "no_eval_results"

            # ── 2b. Inject prompt_repetition score from dedup result ─────────
            # Injects an OBSERVABILITY-ONLY score for audit-trail visibility.
            # The prompt_repetition policy thresholds are set to PERMISSIVE-only
            # (all scores 1-9 → PERMISSIVE) so this dimension never drives a
            # NORMATIVE or COERCIVE verdict through any evaluation path.
            # Scores: 1=unique, 3=near-duplicate (sim≥threshold), 5=exact match.
            if _dedup_result is not None and _dedup_result.repetition_score > 1:
                rep_score  = _dedup_result.repetition_score
                rep_status = "WARNING"   # informational only — not used for enforcement
                rep_input  = EvalInput(
                    dimension="prompt_repetition",
                    score=rep_score,
                    status=rep_status,
                    provider="prompt_deduplicator",
                )
                # Replace GS placeholder if present, otherwise append
                replaced_idx = next(
                    (i for i, ei in enumerate(eval_inputs)
                     if ei.dimension == "prompt_repetition"),
                    None,
                )
                if replaced_idx is not None:
                    eval_inputs[replaced_idx] = rep_input
                else:
                    eval_inputs.append(rep_input)
                logger.debug(
                    f"[Dedup] Repetition annotated prompt_repetition="
                    f"{_dedup_result.repetition_score} "
                    f"exact={_dedup_result.is_exact_duplicate} "
                    f"sim={_dedup_result.similarity:.3f}"
                )

        # ── 3. Fail-safe path ─────────────────────────────────────────────────
        if _fail_reason is not None:
            response = self._fail_safe(request.request_id, _fail_reason)
            self._write_audit(
                request=request,
                response=response,
                interaction_id=interaction_id,
                latency_ms=(time.monotonic() - _t0) * 1000,
                fail_reason=_fail_reason,
                turn_number=None,
                session_verdict=None,
                session_escalated=False,
            )
            return response

        # ── 4. Verdict synthesis — chain executor or flat MRA ────────────────
        pack_traces  = []
        chain_hitl   = False
        chain_hitl_packs: list = []

        if self._chain_executor is not None:
            # ── 4a. Pack-aware chain execution ───────────────────────────────
            # Retrieve current session trust score for threshold step resolution.
            session_trust = None
            if request.session_id and self._session_store is not None:
                state = self._session_store.get(request.session_id, request.app_id)
                if state:
                    session_trust = state.trust_score

            # Fetch per-app trust score from tracker (None if app not yet seen)
            app_trust = None
            if self._app_trust_tracker is not None and request.app_id:
                app_trust = self._app_trust_tracker.get_score(request.app_id)

            chain_result = self._chain_executor.execute(
                request_id=request.request_id,
                role=request.role.value,
                metadata=request.metadata,
                gs_results=eval_inputs,
                deferred_dims=self._deferred_dims,
                session_trust_score=session_trust,
                app_trust_score=app_trust,
                app_id=request.app_id,
                session_id=request.session_id,
            )
            pack_traces   = self._chain_executor.pack_traces_as_dicts(
                chain_result.pack_traces
            )
            chain_hitl       = chain_result.hitl
            chain_hitl_packs = chain_result.hitl_packs
            # Attach the pack trace to the turn response so it flows through
            # to the API response and is available to the finapp trace panel.
            _cr = chain_result.response
            turn_response = PolicyEvaluateResponse(
                request_id=_cr.request_id,
                verdict=_cr.verdict,
                violations=list(_cr.violations),
                remediations=list(_cr.remediations),
                failed_evaluations=list(_cr.failed_evaluations),
                policies=list(_cr.policies),
                pack_trace=pack_traces,
            )
            logger.info(
                f"Chain verdict={turn_response.verdict.value} "
                f"packs_active={sum(1 for t in chain_result.pack_traces if t.activated)} "
                f"hitl={chain_hitl} "
                f"app={request.app_id}"
            )
        else:
            # ── 4b. Flat MRA synthesis (original behaviour) ───────────────────
            turn_response = synthesise(
                request_id=request.request_id,
                evaluations=eval_inputs,
                deferred_dims=self._deferred_dims,
                policy_registry=self._policy_registry,
                app_id=request.app_id,
                session_id=request.session_id,
                role=request.role.value,
            )
            logger.info(
                f"Turn verdict={turn_response.verdict.value} "
                f"violations={len(turn_response.violations)} "
                f"app={request.app_id}"
            )

        # ── 5. Session accumulation (Phase 4) ─────────────────────────────────
        session_verdict:   Optional[Verdict] = None
        session_escalated: bool              = False
        turn_number:       Optional[int]     = None
        final_response = turn_response

        if request.session_id and self._session_store is not None:
            try:
                final_response, session_verdict, session_escalated, turn_number = \
                    self._apply_session(request, eval_inputs, turn_response, chain_result if self._chain_executor else None)
            except Exception as exc:
                # Session accumulation must never degrade the per-turn verdict.
                # Fall back to the turn result and log for investigation.
                logger.exception(
                    f"Session accumulation failed for session_id={request.session_id} "
                    f"request_id={request.request_id} — falling back to turn verdict. "
                    f"Error: {exc}"
                )
                final_response = turn_response
        elif request.session_id and self._session_store is None:
            logger.warning(
                f"session_id={request.session_id} supplied but no SessionStore "
                f"configured — evaluating statelessly"
            )

        # ── 5a2. Collusion accumulator (Phase 2b) ─────────────────────────────
        # Feed the cheap deterministic collusion 'gate's eyes' on agent_to_agent
        # messages: a denial on the channel followed by re-requests / reworded pushes
        # of the same intent. The async detector fleet confirms and trips the breaker.
        #
        # IMPORTANT: skip the SANCTIONED council / negotiation tools (_A2A_SENTINEL_TOOLS).
        # Those represent governed deliberation — reviewers voting and a coordinator
        # re-casting after a split — which the conflict/deadlock SYNC path owns. Counting
        # them as collusion is a false positive (a council reworking a denied item is NOT
        # control-evasion). Collusion watches A2A coordination OUTSIDE the council.
        _cl_tool = ((request.metadata or {}).get("tool_name", "")
                    if isinstance(request.metadata, dict) else "")
        if (request.role.value == "agent_to_agent"
                and request.session_id
                and self._session_store is not None
                and request.app_id not in _VIZ_APPS
                and _cl_tool not in self._A2A_SENTINEL_TOOLS):
            try:
                _cl = self._session_store.accumulate_collusion(
                    request.session_id, request.app_id, request.content or "")
                _cv, _cr = _cl.assess_collusion()
                # Phase 3: sync fast-track on the FIRST WATCH — uphold/decline in-line.
                _cl_dir = None
                if (_SYNC_FASTTRACK and _cv == "WATCH"
                        and "collusion" not in _cl.fasttrack_done):
                    _cstream = [str(e.get("content", "")) for e in _cl.collusion_events][-40:]
                    _oc, _ocr, _lat = self._fast_track_watch("collusion", _cstream)
                    logger.warning("[fast-track] session=%s collusion WATCH -> %s (%.0fms): %s",
                                   request.session_id, _oc, _lat, _ocr[:80])
                    if _oc in ("UPHOLD", "FALLBACK_HITL"):
                        _cv, _cr = "CONFIRMED", (f"sync fast-track "
                                                 f"{'upheld' if _oc == 'UPHOLD' else 'fail-closed'}: {_ocr}")
                        _cl_dir = self._build_fasttrack_directive("collusion", _ocr,
                                                                  fallback=(_oc == "FALLBACK_HITL"))
                    elif _oc == "DECLINE":
                        _cv, _cr = "CLEAR", f"sync fast-track declined (agent): {_ocr}"
                    _cl.fasttrack_done.append("collusion")
                    self._session_store.put(_cl)
                if _cl.risk_flags.get("collusion") != _cv:
                    self._session_store.set_risk_verdict(
                        request.session_id, request.app_id, _cv, _cr, risk_class="collusion")
                if _cl_dir is not None:
                    self.receive_directive(request.session_id, _cl_dir)
                if _cv != "CLEAR":
                    logger.info(f"[emergent-risk] session={request.session_id} collusion={_cv}: {_cr}")
            except Exception as _cl_exc:
                logger.debug(f"[orchestrator] collusion accumulator skipped (non-fatal): {_cl_exc}")

        # ── 5a3. Conflict accumulator (Phase 2c, ASYNC mode only) ──────────────
        # When the SYNC Sentinel path is DISABLED (PE_A2A_SYNC_SENTINEL=false), the
        # async conflict detector owns deadlock instead. Accumulate the SANCTIONED
        # council transcript (_A2A_SENTINEL_TOOLS — the deliberation tools) and flag
        # `conflict` WATCH once enough rounds accrue, so the loop can adjudicate and
        # raise HITL via the directive queue. When sync is enabled (default) this is a
        # no-op — the sync path below owns conflict, no double-firing.
        if (not _A2A_SYNC_SENTINEL
                and request.role.value == "agent_to_agent"
                and request.session_id
                and self._session_store is not None
                and request.app_id not in _VIZ_APPS
                and _cl_tool in self._A2A_SENTINEL_TOOLS):
            try:
                _cf = self._session_store.accumulate_conflict(
                    request.session_id, request.app_id, request.content or "")
                _cfv, _cfr = _cf.assess_conflict()
                if _cf.risk_flags.get("conflict") != _cfv:
                    self._session_store.set_risk_verdict(
                        request.session_id, request.app_id, _cfv, _cfr, risk_class="conflict")
                if _cfv != "CLEAR":
                    logger.info(f"[emergent-risk] session={request.session_id} conflict={_cfv}: {_cfr}")
            except Exception as _cf_exc:
                logger.debug(f"[orchestrator] conflict accumulator skipped (non-fatal): {_cf_exc}")

        # ── 5b. A2A Sentinel sync evaluation ──────────────────────────────────
        # When an agent_to_agent message accumulates to the cap, invoke Sentinel
        # synchronously to validate whether the exchange represents a genuine
        # conflict or deadlock.  A NORMATIVE verdict queues a directive that step
        # 8c will attach to the current response — the SDK then raises HITLInterrupt.
        #
        # Sync mode only — no Trust Radar post, no VerdictStore write.
        # The async observer path (SentinelWebhookForwarder, step 9) is completely
        # decoupled and fires on its own independent trigger conditions.
        # Gated by PE_A2A_SYNC_SENTINEL (Phase 2c): set false to hand conflict to the
        # async detector loop (5a3 above) instead of this in-line sync call.
        if (
            _A2A_SYNC_SENTINEL
            and request.role.value == "agent_to_agent"
            and self._sentinel_sync_client is not None
            and request.session_id
            and self._session_store is not None
            and request.app_id not in _VIZ_APPS
        ):
            try:
                # Extract tool_name from metadata so _check_a2a_sentinel can filter
                # out non-negotiation A2A tools (routing tools vs iterative rounds).
                _a2a_meta      = (request.metadata or {}) if isinstance(request.metadata, dict) else {}
                _a2a_tool_name = _a2a_meta.get("tool_name", "")
                _a2a_directive = self._check_a2a_sentinel(
                    session_id = request.session_id,
                    content    = request.content or "",
                    tool_name  = _a2a_tool_name,
                )
                if _a2a_directive is not None:
                    self.receive_directive(request.session_id, _a2a_directive)
                    # Persist Sentinel's precheck as a real audit record so it shows
                    # up as a captured actor (coordinator → sentinel) in the Eval
                    # Trail, Agent Activity, and Interaction diagram — not just
                    # implicitly via the HITL trigger it raised.
                    self._write_sentinel_audit(request, _a2a_directive, turn_number)
            except Exception as _a2a_exc:
                logger.warning(
                    f"[orchestrator] A2A Sentinel check failed (non-fatal): {_a2a_exc}"
                )

        # ── 6. App Trust Score update ─────────────────────────────────────────
        # App trust reflects the APP's own behaviour — it only moves on roles
        # where the app (not the user) is responsible for the content:
        #
        #   llm_response    — the app generated harmful output
        #   agent_to_agent  — internal agent communication violated policy
        #
        # tool_call COERCIVE is user-driven malicious intent executed through
        # the tool layer — that is the USER's fault, not the app's, so it
        # reduces session trust (handled in _apply_session) but NOT app trust.
        # user_prompt is already excluded for the same reason.
        #
        # This is non-blocking — tracker failure must not affect the response.
        _APP_TRUST_ROLES = {"llm_response", "agent_to_agent"}
        if (self._app_trust_tracker is not None
                and request.app_id
                and request.role.value in _APP_TRUST_ROLES):
            try:
                self._app_trust_tracker.record(
                    app_id=request.app_id,
                    verdict=final_response.verdict.value,
                )
            except Exception as exc:
                logger.warning(f"AppTrustTracker.record failed (non-fatal): {exc}")

        # ── 7. Audit ──────────────────────────────────────────────────────────
        self._write_audit(
            request=request,
            response=final_response,
            interaction_id=interaction_id,
            latency_ms=(time.monotonic() - _t0) * 1000,
            fail_reason=None,
            turn_number=turn_number,
            session_verdict=session_verdict.value if session_verdict else None,
            session_escalated=session_escalated,
            eval_inputs=eval_inputs,
            pack_traces=pack_traces,
        )

        # ── 8. HITL alert emission ─────────────────────────────────────────────
        # Emit a PlatformAlert(category=HITL) whenever the ChainExecutor signals
        # that one or more packs with hitl_trigger=true produced a non-PERMISSIVE
        # verdict.  The alert appears in the PAP dashboard's Platform Alerts panel
        # and is pushed to connected SSE clients in real time.
        #
        # This step is non-blocking and non-fatal — a failed write never
        # degrades the evaluation response returned to the SDK caller.
        if chain_hitl and self._hitl_alert_writer is not None:
            self._hitl_alert_writer.emit(
                interaction_id=interaction_id,
                app_id=request.app_id,
                session_id=request.session_id,
                hitl_packs=chain_hitl_packs,
                violations=list(final_response.violations),
                verdict=final_response.verdict.value,
            )

        # ── 8b. Governance alert emission (zero-tolerance safety net) ──────────
        # For every zero-tolerance dimension that produced a COERCIVE per-dimension
        # verdict, emit a POLICY_VIOLATION governance alert immediately — without
        # waiting for the Sentinel window.  This is the PE's direct safety net;
        # INCIDENT / ADVISORY tiers are raised by Sentinel after cross-turn analysis.
        #
        # eval_inputs holds the per-dimension results from the Guardrail Service.
        # Each EvalInput.status reflects the GS verdict for that dimension alone.
        # We emit one alert per breaching dimension so each can be deduplicated,
        # reviewed, and resolved independently.
        #
        # Non-blocking and non-fatal — a failed write never affects the response.
        if self._governance_alerts_writer is not None:
            _gov_writer = self._governance_alerts_writer
            for _ei in eval_inputs:
                # Compute the PE per-dimension verdict (COERCIVE/NORMATIVE/PERMISSIVE)
                # from the policy registry rather than using the GS raw status field
                # (PASS/WARNING/FLAGGED), which uses different bands and would miss
                # zero-tolerance breaches where the COERCIVE policy threshold sits
                # below the GS's FLAGGED threshold (e.g. score=4 → COERCIVE in PE).
                if _ei.score is None:
                    continue
                _pe_dim_verdict, _ = self._policy_registry.evaluate(
                    _ei.dimension, _ei.score, request.app_id
                )
                if _gov_writer.should_alert(_ei.dimension, _pe_dim_verdict):
                    # Determine which pack enforces this dimension (best-effort)
                    _pack_id: Optional[str] = None
                    for _pt in (getattr(chain_result, "pack_traces", []) if self._chain_executor else []):
                        if hasattr(_pt, "pack_id"):
                            _pack_id = _pt.pack_id
                            break
                    try:
                        _gov_writer.emit(
                            interaction_id = interaction_id,
                            app_id         = request.app_id,
                            session_id     = request.session_id,
                            role           = request.role.value,
                            dimension      = _ei.dimension,
                            dim_score      = _ei.score,
                            pack_id        = _pack_id,
                            violations     = list(final_response.violations),
                            final_verdict  = final_response.verdict.value,
                        )
                    except Exception as _gov_exc:
                        logger.error(
                            f"[gov_alerts] Emit failed for dimension={_ei.dimension}: {_gov_exc}"
                        )

        # ── 8c. Attach directives to response ─────────────────────────────────
        # Two directive sources are merged:
        #   (a) chain_hitl=True from a policy pack with hitl_trigger=True
        #       → add a PE-originated REQUIRE_HUMAN_REVIEW directive
        #   (b) Pending directives queued by Sentinel via receive_directive()
        #       → drained from _pending_directives[session_id]
        # The SDK processes directives in priority order; having both is safe.
        _outbound_directives: List[Directive] = list(final_response.directives)

        if chain_hitl:
            # Determine the HITL category from the triggering pack ID(s)
            # (sanctions packs → SANCTIONS_REVIEW; default → AGENT_ARBITRATION)
            _hitl_cat = HITLCategory.SANCTIONS_REVIEW if any(
                "sanction" in p.lower() for p in chain_hitl_packs
            ) else HITLCategory.AGENT_ARBITRATION
            _chain_directive = Directive(
                action   = DirectiveAction.REQUIRE_HUMAN_REVIEW,
                category = _hitl_cat,
                reason   = (
                    f"Policy pack(s) {chain_hitl_packs} produced a "
                    f"{final_response.verdict.value} verdict with hitl_trigger=True"
                ),
                source   = DirectiveSource.PE,
            )
            _outbound_directives.append(_chain_directive)
            logger.info(
                f"[orchestrator] PE directive: REQUIRE_HUMAN_REVIEW "
                f"category={_hitl_cat.value} packs={chain_hitl_packs}"
            )

        _pending = self._drain_pending_directives(request.session_id)
        if _pending:
            _outbound_directives.extend(_pending)
            logger.info(
                f"[orchestrator] Attaching {len(_pending)} Sentinel directive(s) "
                f"to response — session={request.session_id}"
            )

        if _outbound_directives:
            final_response = final_response.model_copy(
                update={"directives": _outbound_directives}
            )

        # ── 9. Sentinel webhook ────────────────────────────────────────────────
        # Fire an async webhook to the Sentinel Agent when:
        #   (a) verdict == COERCIVE    — high-severity event, immediate analysis
        #   (b) eval_counter % N == 0 — periodic scan via GLOBAL counter
        #
        # Using a global counter (not per-session turn_number) means Sentinel fires
        # within N evaluations of PE startup regardless of session.  Sentinel's pull
        # then grabs the last M audit events — naturally covering any events written
        # before Sentinel was running.  Non-blocking and non-fatal.
        if self._sentinel_webhook_forwarder is not None:
            _is_coercive = (final_response.verdict == Verdict.COERCIVE)
            self._sentinel_eval_counter += 1
            _is_n_eval   = (self._sentinel_eval_counter % self._sentinel_trigger_n == 0)
            if _is_coercive or _is_n_eval:
                _trigger_reason = "COERCIVE" if _is_coercive else "N_EVAL"
                self._sentinel_webhook_forwarder.emit(
                    interaction_id=interaction_id,
                    session_id=request.session_id,
                    app_id=request.app_id,
                    role=request.role.value,
                    verdict=final_response.verdict.value,
                    violations=list(final_response.violations),
                    turn_number=turn_number,
                    trigger_reason=_trigger_reason,
                )

        return final_response

    # ── Session accumulation ───────────────────────────────────────────────────

    def _apply_session(
        self,
        request:       PolicyEvaluateRequest,
        eval_inputs:   list,
        turn_response: PolicyEvaluateResponse,
        chain_result=None,
    ):
        """
        Merge this turn's scores into session state and compute the session-level
        verdict for audit/observability purposes.

        IMPORTANT — enforcement boundary:
        The API response always reflects the per-turn verdict. Session escalation
        is recorded in the audit trail and visible in the governance trace panel
        but does NOT modify the verdict returned to the calling application.
        Enforcement decisions based on session patterns are a future-phase concern
        (COGS layer); the app should not need to interpret session state.

        Returns (turn_response, session_verdict, session_escalated, turn_number).
        """
        # Build dimension → score map from this turn's eval_inputs
        turn_scores = {
            ei.dimension: ei.score
            for ei in eval_inputs
            if ei.score is not None and ei.dimension not in self._deferred_dims
        }

        state = self._session_store.get_or_create(request.session_id, request.app_id)

        # ── Turn-count restoration after session TTL expiry / PE restart ─────
        # InMemorySessionStore evicts sessions after TTL (default 30 min).  When
        # get_or_create returns a freshly-created state (turn_count==0) for a
        # session that already has rows in audit_interaction_events, the new
        # state would restart turn numbering at 1/2/… instead of continuing
        # from where it left off.  Look up the DB max to resume correctly.
        if state.turn_count == 0 and self._audit_writer is not None:
            try:
                _db_max = self._audit_writer.get_session_max_turn(request.session_id)
                if _db_max is not None and _db_max > 0:
                    state.turn_count = _db_max
                    logger.info(
                        "[PE] Session %s: turn_count restored to %d from "
                        "audit_interaction_events (session recreated after TTL expiry)",
                        request.session_id, _db_max,
                    )
            except Exception as _tc_exc:
                logger.debug("[PE] turn_count restoration failed: %s", _tc_exc)

        # ── Multi-turn sliding/salami attack detection ────────────────────────
        # Only runs on user_prompt turns — the source of multi-turn injection
        # sequences.  The detector updates state.prompt_window and returns
        # elevated scores for prompt_injection / jailbreak when cross-turn
        # patterns are detected.  Elevated scores are merged into turn_scores
        # so they feed the normal session accumulation path below.
        if (request.role.value == "user_prompt"
                and self._session_attack_detector is not None):
            try:
                augmented = self._session_attack_detector.analyse(
                    prompt=request.content,
                    state=state,
                    current_turn_scores=turn_scores,
                )
                for dim, elevated in augmented.items():
                    if elevated > turn_scores.get(dim, 0):
                        turn_scores[dim] = elevated
            except Exception as det_exc:
                # SessionAttackDetector already catches internally; this is a
                # belt-and-suspenders guard — detector failure must never degrade
                # the evaluation response.
                logger.warning(
                    f"[SessionAttackDetector] Outer guard caught error "
                    f"(session_id={request.session_id}): {det_exc}"
                )

        # Collect high-risk policy IDs from this turn's chain result.
        # A policy is high-risk when severity=3 and verdict=COERCIVE.
        # The chain executor exposes this via pack_traces — any dim result that
        # is COERCIVE in a pack whose policy has severity 3 qualifies.
        high_risk_ids: list = []
        if chain_result is not None:
            for trace in chain_result.pack_traces:
                if trace.pack_verdict == "COERCIVE":
                    for dim in trace.dimensions_evaluated:
                        policy = self._policy_registry.get(dim)
                        if policy and policy.severity == 3:
                            high_risk_ids.append(dim)

        # Only count user_prompt as a new conversational turn.
        # agent_to_agent, tool_call, and llm_response evaluations all belong
        # to the same turn as the user_prompt that triggered them — they
        # contribute dimension scores but must not advance the turn counter.
        state.record_turn(
            turn_scores,
            turn_response.verdict.value,
            interception_point=request.role.value,
            increment_turn=(request.role.value == "user_prompt"),
            high_risk_policy_ids=high_risk_ids or None,
        )
        self._session_store.put(state)

        # Prefer caller-supplied turn_number (SDK or showcase runner) over the
        # PE's own self-maintained counter.  Removes PE's authority over turn
        # counting — the caller is always the authoritative source of truth.
        # Falls back to state.turn_count for legacy / SDK-less callers.
        turn_number = (
            int(request.turn_number)
            if request.turn_number is not None
            else state.turn_count
        )

        # Re-evaluate accumulated worst scores to compute session-level verdict.
        # Used for audit visibility only — does not affect the returned verdict.
        session_eval_inputs = []
        for dim, score in state.dimension_scores.items():
            verdict, _ = self._policy_registry.evaluate(dim, score, request.app_id)
            status = (
                "FLAGGED" if verdict == "COERCIVE"
                else "WARNING" if verdict == "NORMATIVE"
                else "PASS"
            )
            session_eval_inputs.append(EvalInput(
                dimension=dim,
                score=score,
                status=status,
                provider="session_accumulation",
            ))

        if not session_eval_inputs:
            return turn_response, turn_response.verdict, False, turn_number

        session_response  = synthesise(
            request_id=request.request_id,
            evaluations=session_eval_inputs,
            deferred_dims=self._deferred_dims,
            policy_registry=self._policy_registry,
            app_id=request.app_id,
            session_id=request.session_id,
        )

        session_verdict   = session_response.verdict
        turn_rank         = _VERDICT_RANK[turn_response.verdict]
        session_rank      = _VERDICT_RANK[session_verdict]
        session_escalated = session_rank > turn_rank

        if session_escalated:
            state.escalated = True
            self._session_store.put(state)
            logger.info(
                f"SESSION ESCALATION recorded (audit only) | "
                f"session_id={request.session_id} turn={turn_number} "
                f"turn_verdict={turn_response.verdict.value} "
                f"session_verdict={session_verdict.value} "
                f"— API response reflects per-turn verdict"
            )

        # ── Build session summary and attach to the per-turn response ─────────
        # This gives the calling application a live session health snapshot
        # without changing the enforcement verdict.
        worst_verdict_str = (
            max(state.turn_verdicts,
                key=lambda v: _VERDICT_RANK.get(Verdict(v), 0))
            if state.turn_verdicts else "PERMISSIVE"
        )
        summary = SessionSummary(
            turn_count=turn_number,
            composite_score=state.trust_score,
            worst_verdict=worst_verdict_str,
            dimension_scores=dict(state.dimension_scores),
            escalated=state.escalated,
        )
        # Construct a new response with the summary attached.
        # The verdict and all enforcement fields remain unchanged.
        # pack_trace is carried forward unchanged — it was set in step 4.
        response_with_summary = PolicyEvaluateResponse(
            request_id=turn_response.request_id,
            verdict=turn_response.verdict,
            violations=list(turn_response.violations),
            remediations=list(turn_response.remediations),
            failed_evaluations=list(turn_response.failed_evaluations),
            policies=list(turn_response.policies),
            session_summary=summary,
            pack_trace=list(turn_response.pack_trace),
        )

        # Always return the per-turn verdict — session verdict is audit-only.
        return response_with_summary, session_verdict, session_escalated, turn_number

    # ── Fail-safe helper ───────────────────────────────────────────────────────

    def _fail_safe(self, request_id: str, reason: str) -> PolicyEvaluateResponse:
        """
        Returns a safe verdict when the Guardrail Service cannot be reached.
        fail_mode=open   → PERMISSIVE
        fail_mode=closed → COERCIVE
        """
        if self._fail_mode == FAIL_CLOSED:
            verdict     = Verdict.COERCIVE
            violation   = (
                f"guardrail_unavailable [{reason}]: content blocked — "
                f"evaluation could not be completed"
            )
            remediation = (
                "The Guardrail Service is temporarily unavailable. "
                "The request has been blocked as a precaution (FAIL_MODE=closed). "
                "Retry when the service is restored."
            )
            logger.error(
                f"FAIL-CLOSED | verdict=COERCIVE | reason={reason} | "
                f"request_id={request_id}"
            )
        else:
            verdict     = Verdict.PERMISSIVE
            violation   = None
            remediation = None
            logger.warning(
                f"FAIL-OPEN | verdict=PERMISSIVE | reason={reason} | "
                f"request_id={request_id} | "
                f"WARNING: content passed WITHOUT guardrail evaluation"
            )

        return PolicyEvaluateResponse(
            request_id=request_id,
            verdict=verdict,
            violations=[violation] if violation else [],
            remediations=[remediation] if remediation else [],
            failed_evaluations=[],
            policies=[],
        )

    # ── Audit helper ───────────────────────────────────────────────────────────

    def _write_audit(
        self,
        request:           PolicyEvaluateRequest,
        response:          PolicyEvaluateResponse,
        interaction_id:    str,
        latency_ms:        float,
        fail_reason:       Optional[str],
        turn_number:       Optional[int],
        session_verdict:   Optional[str],
        session_escalated: bool,
        eval_inputs:       list = [],
        pack_traces:       list = [],
    ) -> None:
        if self._audit_writer is None:
            return
        try:
            record = AuditRecord(
                interaction_id=interaction_id,
                request_id=response.request_id,
                app_id=request.app_id,
                timestamp=datetime.now(timezone.utc).isoformat(),
                role=request.role.value,
                verdict=response.verdict.value,
                violations=list(response.violations),
                remediations=list(response.remediations),
                session_id=request.session_id,
                agent_id=request.agent_id,
                metadata=request.metadata,
                turn_number=turn_number,
                session_verdict=session_verdict,
                session_escalated=session_escalated,
                latency_ms=round(latency_ms, 1),
                # Write ALL dimension scores — not just failed — so observability
                # tools can compute safety/toxicity averages across the full range.
                # Failed evaluations are identifiable via status != "PASS".
                dimension_results=[
                    {
                        "dimension": ei.dimension,
                        "score":     ei.score,
                        "status":    ei.status,
                        "provider":  ei.provider,
                    }
                    for ei in eval_inputs
                ] if eval_inputs else [
                    {
                        "dimension": fe.dimension,
                        "score":     fe.score,
                        "status":    fe.status,
                        "provider":  fe.provider,
                    }
                    for fe in response.failed_evaluations
                ],
                fail_reason=fail_reason,
                pack_trace=pack_traces,
                enforcement_action={
                    "PERMISSIVE": "pass",
                    "NORMATIVE":  "warned",
                    "COERCIVE":   "blocked",
                }.get(response.verdict.value, "unknown"),
                content=request.content,
            )
            self._audit_writer.write(record)
        except Exception as exc:
            logger.error(
                f"Audit write failed for request_id={request.request_id}: {exc}"
            )

    def _write_sentinel_audit(
        self,
        request:     PolicyEvaluateRequest,
        directive:   "Directive",
        turn_number: Optional[int],
    ) -> None:
        """
        Persist Sentinel's cross-turn precheck as a real audit record (Option B).

        Written as an agent_to_agent edge coordinator → sentinel so the SqliteWriter
        derives the right interaction edge and Sentinel appears as a captured actor
        in every view. Verdict = Sentinel's own verdict (NORMATIVE when it opened a
        review). Best-effort: never breaks the evaluation on failure.
        """
        if self._audit_writer is None:
            return
        try:
            meta      = directive.metadata or {}
            verdict   = (meta.get("sentinel_verdict") or "NORMATIVE").upper()
            pattern   = meta.get("sentinel_pattern")   or "agent_conflict"
            reasoning = meta.get("sentinel_reasoning") or directive.reason or ""
            record = AuditRecord(
                interaction_id=str(uuid.uuid4()),
                request_id=request.request_id,
                app_id=request.app_id,
                timestamp=datetime.now(timezone.utc).isoformat(),
                role="agent_to_agent",
                verdict=verdict,
                violations=[],
                remediations=[],
                session_id=request.session_id,
                agent_id="sentinel",
                metadata={
                    "agent_name":           "sentinel",
                    "source_agent":         "coordinator",
                    "sentinel_pattern":     pattern,
                    "sentinel_reasoning":   str(reasoning)[:300],
                    "is_sentinel_precheck": True,
                },
                turn_number=turn_number,
                session_verdict=None,
                session_escalated=False,
                latency_ms=0.0,
                dimension_results=[],
                fail_reason=None,
                pack_trace=[],
                enforcement_action="warned" if verdict == "NORMATIVE" else "pass",
                content=f"Sentinel cross-turn precheck — {pattern}: {str(reasoning)[:200]}",
            )
            self._audit_writer.write(record)
            logger.info(
                f"[orchestrator] Sentinel audit record written "
                f"(verdict={verdict}, pattern={pattern}) session={request.session_id}"
            )
        except Exception as exc:
            logger.error(f"Sentinel audit write failed: {exc}")
