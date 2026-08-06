"""
Session Store — multi-turn session correlation (SRS §4.5.5).

Design
------
The session store maintains per-session state across evaluation turns so the
Policy Engine can detect distributed violations — patterns that do not exceed
a threshold in any individual turn but cross it cumulatively.

Session trust score
-------------------
A delta-based scalar (starts 1.0) that decays on COERCIVE/NORMATIVE verdicts
and recovers slowly on PERMISSIVE ones.  Consumed by the session_guard pack's
session_risk_scorer evaluator via the chain_executor's synthetic scorer — so
sessions with repeated violations automatically receive stricter thresholds
without any special-case code in the orchestrator.

High-risk policy tracking
-------------------------
Any policy with severity=3 that returns COERCIVE is considered a high-risk
outcome.  Its ID is recorded in high_risk_policies so subsequent evaluations
can be informed by what has already fired.  This replaces the old blocked_tools
list — the policy model (not a tool registry) now drives what is "high risk."

Coercive breakdown
------------------
coercive_by_interception and coercive_by_policy track COERCIVE counts per
interception point and per policy ID respectively.  These are cheap incremental
counters written on every COERCIVE outcome and promoted verbatim into the
SessionSummary on compaction — no audit-store read needed.

Session compaction
------------------
When a session ends (TTL expiry, explicit termination, or trust-floor reached)
it is compacted into a SessionSummary record before being deleted from the
live store.  SessionSummary is a cheap, permanent, cross-session-queryable
row that answers "what happened in this session?" without touching the audit
trail.

Implementations
---------------
InMemorySessionStore  — zero dependencies, suitable for dev / single-instance PE.
                        State is lost on PE restart.  Good for testing.

DiskSessionStore      — JSON files under a configurable directory.  Survives PE
                        restarts.  Not suitable for multi-instance deployments
                        (no cross-process locking).  Use Redis/Valkey for that.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Session policy (externalised from hardcoded constants) ─────────────────────
#
# Defaults are used when DB is unavailable (PE first-boot race condition only).
# Call load_session_policy_from_db() to reload at runtime.

_DEFAULT_TRUST_DELTA = {
    "COERCIVE":   -0.20,
    "NORMATIVE":  -0.05,
    "PERMISSIVE": +0.02,
}
_DEFAULT_TRUST_FLOOR       = 0.20
_DEFAULT_TRUST_CEILING     = 1.0
_DEFAULT_TTL_SECONDS       = 1800.0
_DEFAULT_DEDUP_EXACT_BLOCK = True
_DEFAULT_DEDUP_THRESHOLD   = 0.92
_DEFAULT_DEDUP_WINDOW      = 50
_DEFAULT_REPETITION_PENALTY= -0.05

# Live values (mutated by load_session_policy)
_SESSION_TRUST_DELTA: Dict[str, float] = dict(_DEFAULT_TRUST_DELTA)
_SESSION_TRUST_FLOOR:   float = _DEFAULT_TRUST_FLOOR
_SESSION_TRUST_CEILING: float = _DEFAULT_TRUST_CEILING
_SESSION_TTL_SECONDS:   float = _DEFAULT_TTL_SECONDS

# True once the session policy has been successfully read from config_session_policy.
# The import-time load can lose a race with the entrypoint seed copy (or a not-yet-ready
# DB) and fall back to built-in defaults — which would silently apply the DEFAULT trust
# deltas (e.g. NORMATIVE -0.05) instead of the operator's configured values for the whole
# process lifetime. While this stays False the scorer lazily retries the DB load on use,
# so a boot-time miss self-heals on the first user_prompt instead of stranding the default.
_POLICY_LOADED_FROM_DB: bool = False

# Dedup settings exposed for the orchestrator / PromptCache
SESSION_DEDUP_EXACT_BLOCK:  bool  = _DEFAULT_DEDUP_EXACT_BLOCK
SESSION_DEDUP_THRESHOLD:    float = _DEFAULT_DEDUP_THRESHOLD
SESSION_DEDUP_WINDOW:       int   = _DEFAULT_DEDUP_WINDOW
SESSION_REPETITION_PENALTY: float = _DEFAULT_REPETITION_PENALTY

# ── Emergent-risk / structuring accumulator config (Phase 1) ──────────────────
# Deliberately OVER-sensitive deterministic floor: WATCH at a low bar (defer to the
# agentic layer), CONFIRM only on a clearly-clustered pattern. All env-tunable.
_PAYMENT_WINDOW:        int   = int(os.getenv("PAYMENT_WINDOW", "25"))          # bounded payment log
_STRUCT_REPORT_LIMIT:   float = float(os.getenv("STRUCT_REPORT_LIMIT", "10000"))# reporting threshold
_STRUCT_BAND_LO:        float = float(os.getenv("STRUCT_BAND_LO", "8000"))      # "just under" band floor
_STRUCT_WINDOW_S:       float = float(os.getenv("STRUCT_WINDOW_S", "86400"))    # look-back window (s)
_STRUCT_WATCH_N:        int   = int(os.getenv("STRUCT_WATCH_N", "2"))           # >=N band-hugging -> WATCH
_STRUCT_CONFIRM_N:      int   = int(os.getenv("STRUCT_CONFIRM_N", "3"))         # >=N same-recipient -> CONFIRMED

# ── Collusion / control-evasion accumulator config (Phase 2b) ──────────────────
# Cheap deterministic "gate's eyes" for agent-collusion: a denial/refusal on the
# A2A channel followed by re-requests or reworded pushes of the same intent. Like
# structuring, deliberately OVER-sensitive (WATCH at the first re-attempt) and the
# agentic detector confirms or clears.
_COLLUSION_WINDOW:      int   = int(os.getenv("COLLUSION_WINDOW", "40"))         # bounded A2A content log
_COLLUSION_WATCH_N:     int   = int(os.getenv("COLLUSION_WATCH_N", "1"))         # >=N post-denial pushes -> WATCH
_COLLUSION_CONFIRM_N:   int   = int(os.getenv("COLLUSION_CONFIRM_N", "2"))       # >=N post-denial pushes -> CONFIRMED
# Keyword sets (lowercased substring match). Cheap, language-specific; the agentic
# detector is the real arbiter — these only decide whether to spend an LLM call.
_COLLUSION_DENY_MARKERS = (
    "denied", "declined", "rejected", "not approved", "cannot approve",
    "can't approve", "blocked", "refused", "not permitted", "disallow",
)
_COLLUSION_PUSH_MARKERS = (
    "re-approve", "reapprove", "approve", "re-request", "again", "retry",
    "please process", "process the", "instead", "split", "break", "proceed anyway",
    "override", "bypass", "routine", "go ahead", "just this once",
)

# ── Conflict / deadlock accumulator config (Phase 2c) ─────────────────────────
# Cheap deterministic "gate's eyes" for council deadlock when the SYNC Sentinel path
# is disabled (PE_A2A_SYNC_SENTINEL=false). Counts sanctioned council/deliberation
# messages; once enough rounds have accrued without resolution, flag WATCH and let the
# async conflict detector decide whether it is a genuine hung split needing arbitration.
_CONFLICT_WINDOW:       int   = int(os.getenv("CONFLICT_WINDOW", "40"))          # bounded council transcript
_CONFLICT_WATCH_N:      int   = int(os.getenv("CONFLICT_WATCH_N", "6"))          # >=N council msgs -> WATCH

# Standing-risk class severity ranking (worst-of rollup across risk classes).
_RISK_RANK = {"CLEAR": 0, "WATCH": 1, "CONFIRMED": 2}

def _apply_raw_config(raw: dict) -> None:
    """Apply a parsed config dict to the module-level session-policy globals."""
    global _SESSION_TRUST_DELTA, _SESSION_TRUST_FLOOR, _SESSION_TRUST_CEILING
    global _SESSION_TTL_SECONDS
    global SESSION_DEDUP_EXACT_BLOCK, SESSION_DEDUP_THRESHOLD
    global SESSION_DEDUP_WINDOW, SESSION_REPETITION_PENALTY

    deltas = raw.get("trust_deltas", {})
    _SESSION_TRUST_DELTA["COERCIVE"]   = float(deltas.get("COERCIVE",   _DEFAULT_TRUST_DELTA["COERCIVE"]))
    _SESSION_TRUST_DELTA["NORMATIVE"]  = float(deltas.get("NORMATIVE",  _DEFAULT_TRUST_DELTA["NORMATIVE"]))
    _SESSION_TRUST_DELTA["PERMISSIVE"] = float(deltas.get("PERMISSIVE", _DEFAULT_TRUST_DELTA["PERMISSIVE"]))

    _SESSION_TRUST_FLOOR   = float(raw.get("trust_floor",         _DEFAULT_TRUST_FLOOR))
    _SESSION_TRUST_CEILING = float(raw.get("trust_ceiling",       _DEFAULT_TRUST_CEILING))
    _SESSION_TTL_SECONDS   = float(raw.get("session_ttl_seconds", _DEFAULT_TTL_SECONDS))

    dedup = raw.get("dedup", {})
    SESSION_DEDUP_EXACT_BLOCK  = bool(dedup.get("exact_block",          _DEFAULT_DEDUP_EXACT_BLOCK))
    SESSION_DEDUP_THRESHOLD    = float(dedup.get("similarity_threshold", _DEFAULT_DEDUP_THRESHOLD))
    SESSION_DEDUP_WINDOW       = int(dedup.get("window",                 _DEFAULT_DEDUP_WINDOW))
    SESSION_REPETITION_PENALTY = float(dedup.get("repetition_penalty",   _DEFAULT_REPETITION_PENALTY))


def load_session_policy_from_db() -> dict:
    """
    Load session policy from config_session_policy in the shared PAP database.
    Updates module-level globals.  Falls back to built-in defaults if DB
    unavailable (first-boot race condition only — DB is the sole source of truth).
    """
    try:
        from core.database import engine, DB_AVAILABLE
        if not DB_AVAILABLE:
            logger.warning("[SessionPolicy] DB unavailable — using built-in defaults")
            return _build_policy_dict()

        from sqlalchemy import text
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT trust_delta_coercive, trust_delta_normative, trust_delta_permissive, "
                    "       trust_floor, trust_ceiling, session_ttl_seconds, "
                    "       dedup_exact_block, dedup_similarity_threshold, "
                    "       dedup_window, dedup_repetition_penalty "
                    "FROM config_session_policy WHERE id = 1"
                )
            ).fetchone()

        if row is None:
            logger.warning("[SessionPolicy] config_session_policy row missing — using built-in defaults")
            return _build_policy_dict()

        raw = {
            "trust_deltas": {
                "COERCIVE":   row[0],
                "NORMATIVE":  row[1],
                "PERMISSIVE": row[2],
            },
            "trust_floor":          row[3],
            "trust_ceiling":        row[4],
            "session_ttl_seconds":  row[5],
            "dedup": {
                "exact_block":          bool(row[6]),
                "similarity_threshold": row[7],
                "window":               row[8],
                "repetition_penalty":   row[9],
            },
        }
        _apply_raw_config(raw)
        global _POLICY_LOADED_FROM_DB
        _POLICY_LOADED_FROM_DB = True
        logger.info(
            f"[SessionPolicy] Loaded from PAP DB: "
            f"normative_delta={_SESSION_TRUST_DELTA['NORMATIVE']} "
            f"ttl={_SESSION_TTL_SECONDS}s floor={_SESSION_TRUST_FLOOR} "
            f"dedup_threshold={SESSION_DEDUP_THRESHOLD}"
        )
        return _build_policy_dict()

    except Exception as exc:
        logger.warning(f"[SessionPolicy] DB load failed ({exc}) — using built-in defaults")
        return _build_policy_dict()


def save_session_policy(config: dict) -> None:
    """
    Persist session policy to the shared PAP database.
    Raises RuntimeError if DB is unavailable (DB is the sole source of truth).
    """
    from core.database import engine, DB_AVAILABLE
    if not DB_AVAILABLE:
        raise RuntimeError("[SessionPolicy] DB unavailable — cannot save session policy")
    from sqlalchemy import text
    dedup  = config.get("dedup", {})
    deltas = config.get("trust_deltas", {})
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE config_session_policy SET "
                "  trust_delta_coercive = :coercive, "
                "  trust_delta_normative = :normative, "
                "  trust_delta_permissive = :permissive, "
                "  trust_floor = :floor, trust_ceiling = :ceiling, "
                "  session_ttl_seconds = :ttl, "
                "  dedup_exact_block = :exact_block, "
                "  dedup_similarity_threshold = :sim_threshold, "
                "  dedup_window = :window, "
                "  dedup_repetition_penalty = :rep_penalty, "
                "  updated_at = datetime('now'), updated_by = 'pe-api' "
                "WHERE id = 1"
            ),
            {
                "coercive":      deltas.get("COERCIVE",   -0.20),
                "normative":     deltas.get("NORMATIVE",  -0.05),
                "permissive":    deltas.get("PERMISSIVE",  0.02),
                "floor":         config.get("trust_floor",   0.20),
                "ceiling":       config.get("trust_ceiling", 1.0),
                "ttl":           config.get("session_ttl_seconds", 1800),
                "exact_block":   int(dedup.get("exact_block", True)),
                "sim_threshold": dedup.get("similarity_threshold", 0.92),
                "window":        dedup.get("window", 50),
                "rep_penalty":   dedup.get("repetition_penalty", -0.05),
            },
        )
    logger.info("[SessionPolicy] Saved to PAP DB")
    _apply_raw_config(config)


def _build_policy_dict() -> dict:
    """Return the current live config as a plain dict (for GET /v1/session-policy)."""
    return {
        "trust_deltas": dict(_SESSION_TRUST_DELTA),
        "trust_floor":  _SESSION_TRUST_FLOOR,
        "trust_ceiling": _SESSION_TRUST_CEILING,
        "session_ttl_seconds": _SESSION_TTL_SECONDS,
        "dedup": {
            "exact_block":          SESSION_DEDUP_EXACT_BLOCK,
            "similarity_threshold": SESSION_DEDUP_THRESHOLD,
            "window":               SESSION_DEDUP_WINDOW,
            "repetition_penalty":   SESSION_REPETITION_PENALTY,
        },
    }


# Load at import time — try DB first, fall back to YAML
load_session_policy_from_db()

# Interception points tracked in coercive_by_interception
_INTERCEPTION_POINTS = (
    "user_prompt",
    "tool_call",
    "agent_to_agent",
    "llm_response",
)


# ── Session state ──────────────────────────────────────────────────────────────

@dataclass
class SessionState:
    """
    Mutable state for one workflow session.

    Fields
    ------
    session_id              Caller-supplied session identifier.
    app_id                  Application identifier — sessions are isolated per app.
    created_at              Unix epoch seconds when the session was created.
    last_activity           Unix epoch seconds of the most recent turn.
    turn_count              Number of user_prompt turns completed.
    dimension_scores        Running worst score per dimension across all turns.
    turn_verdicts           Ordered list of per-turn verdicts (newest last).
    escalated               True when a session verdict has escalated a turn verdict.
    trust_score             Delta-based session trust score (1.0 → decays → floor).
                            Consumed by the session_guard pack's session_risk_scorer
                            evaluator to tighten thresholds as session risk accumulates.
    min_trust_score         Lowest trust_score reached in this session.  Written into
                            SessionSummary on compaction for cross-session analysis.
    high_risk_policies      IDs of severity-3 policies that returned COERCIVE at least
                            once in this session.  Replaces the old blocked_tools list.
    coercive_by_interception  COERCIVE count per interception point.
                              Promoted to SessionSummary verbatim on compaction.
    coercive_by_policy      COERCIVE count per policy/dimension ID.
                            Promoted to SessionSummary verbatim on compaction.
    """
    session_id:               str
    app_id:                   str
    created_at:               float = field(default_factory=time.time)
    last_activity:            float = field(default_factory=time.time)
    turn_count:               int   = 0
    dimension_scores:         Dict[str, int]  = field(default_factory=dict)
    turn_verdicts:            List[str]       = field(default_factory=list)
    escalated:                bool            = False
    trust_score:              float           = 1.0
    min_trust_score:          float           = 1.0
    high_risk_policies:       List[str]       = field(default_factory=list)
    coercive_by_interception: Dict[str, int]  = field(
        default_factory=lambda: {p: 0 for p in _INTERCEPTION_POINTS}
    )
    coercive_by_policy:       Dict[str, int]  = field(default_factory=dict)

    # ── Prompt deduplication cache (PromptDeduplicator) ───────────────────
    # prompt_hashes      SHA-256 hex digests of every user_prompt in this session,
    #                    bounded by SESSION_DEDUP_WINDOW.  Exact-duplicate detection
    #                    requires only this field (no ML).
    # prompt_embeddings  Corresponding 384-dim unit-norm embedding vectors.
    #                    Empty list [] stored as placeholder when model unavailable.
    #                    Only populated when sentence-transformers is installed.
    prompt_hashes:      List[str]        = field(default_factory=list)
    prompt_embeddings:  List[List[float]]= field(default_factory=list)

    # ── Multi-turn attack detection window (SessionAttackDetector) ────────
    # prompt_window      Raw text of the last N user_prompt turns (bounded by
    #                    SessionAttackDetector.WINDOW_SIZE = 10).  Used by the
    #                    SessionAttackDetector to detect sliding / salami-style
    #                    injection and jailbreak attacks that span multiple turns.
    #                    Never transmitted to the Guardrail Service — PE-internal only.
    prompt_window:      List[str]        = field(default_factory=list)

    # ── Agent-to-agent message accumulation (Sentinel sync eval) ──────────
    # a2a_messages       Raw content strings of every agent_to_agent turn,
    #                    capped at A2A_HITL_THRESHOLD (default 6 = 3 full
    #                    back-and-forth cycles).  Passed verbatim to Sentinel's
    #                    /evaluate-sync endpoint for conflict/deadlock classification.
    #                    Never transmitted to the Guardrail Service — PE-internal only.
    # a2a_hitl_fired     True once Sentinel has been invoked and returned NORMATIVE
    #                    for this session.  Prevents repeated HITL triggers after the
    #                    cap is first hit.
    a2a_messages:       List[str]        = field(default_factory=list)
    a2a_hitl_fired:     bool             = False
    # a2a_tool_counts    How many times each A2A tool (agent) has been invoked this
    #                    session. Used for repetition-based deadlock detection: a
    #                    genuine back-and-forth is two+ agents RE-casting (count>=2),
    #                    which is robust to the raw message total.
    a2a_tool_counts:    Dict[str, int]   = field(default_factory=dict)

    # ── Emergent-risk accumulators + standing risk record (Phase 1) ───────
    # payment_events  Compact log of payment actions this session, each
    #                 {"amount": float, "recipient": str, "ts": float}, bounded
    #                 to the last _PAYMENT_WINDOW entries. The deterministic
    #                 "gate's eyes" digest for structuring (velocity / banding /
    #                 recipient concentration). PE-internal; never sent to the GS.
    payment_events:     List[Dict[str, Any]] = field(default_factory=list)
    # ── Standing risk record (per-session emergent-risk verdict + breaker) ─
    # risk_verdict   CLEAR | WATCH | CONFIRMED — standing emergent-risk verdict.
    #                Phase 1: set by the deterministic accumulators (observational).
    #                Phase 2+: maintained by the async Sentinel fleet from the firehose.
    # risk_reason    One-line rationale for the current verdict.
    # freeze         True = circuit-breaker tripped; the PE gate BLOCKs every
    #                subsequent action in this session until cleared (Part III #4).
    # freeze_reason / freeze_at / freeze_ttl  breaker provenance + optional expiry.
    risk_verdict:       str   = "CLEAR"
    risk_reason:        str   = ""
    freeze:             bool  = False
    freeze_reason:      str   = ""
    freeze_at:          float = 0.0
    freeze_ttl:         float = 0.0
    # ── Per-class standing verdicts (Phase 2b) ────────────────────────────────
    # risk_flags maps each emergent-risk CLASS to its standing verdict, e.g.
    # {"structuring": "CONFIRMED", "collusion": "WATCH"}. risk_verdict/risk_reason
    # above remain the WORST-OF rollup across all classes (back-compat for the gate,
    # UI, and existing callers). The async detector dispatches per class from here.
    risk_flags:         Dict[str, str]       = field(default_factory=dict)
    # risk_reasons  Per-class one-line rationale, parallel to risk_flags. Lets the
    #               worst-of rollup always surface the CORRECT class's reason, even
    #               when the class being updated is not the one that is worst.
    risk_reasons:       Dict[str, str]       = field(default_factory=dict)
    # collusion_events  Bounded log of A2A message contents this session, each
    #                   {"content": str, "ts": float}. The cheap 'gate's eyes'
    #                   digest for collusion / control-evasion (post-denial pushes).
    #                   PE-internal; never sent to the Guardrail Service.
    collusion_events:   List[Dict[str, Any]] = field(default_factory=list)
    # conflict_events  Bounded log of SANCTIONED council/deliberation messages this
    #                  session, each {"content": str, "ts": float}. Feeds the async
    #                  conflict (deadlock) detector when the sync Sentinel path is
    #                  disabled (PE_A2A_SYNC_SENTINEL=false). PE-internal.
    conflict_events:    List[Dict[str, Any]] = field(default_factory=list)
    # fasttrack_done  Risk classes already adjudicated by the Phase-3 SYNC fast-track
    #                 this session — guards against re-firing the inline agentic call on
    #                 every subsequent WATCH eval (decide once, then determinism/async
    #                 escalate if the pattern worsens).
    fasttrack_done:     List[str]             = field(default_factory=list)

    # ── Mutation helpers ───────────────────────────────────────────────────────

    def touch(self) -> None:
        self.last_activity = time.time()

    def record_turn(
        self,
        dimension_scores:  Dict[str, int],
        verdict:           str,
        interception_point: str,
        increment_turn:    bool = False,
        high_risk_policy_ids: Optional[List[str]] = None,
    ) -> None:
        """
        Merge this turn's scores into the running worst-score table and update
        all trust/coercive tracking fields atomically.

        Parameters
        ----------
        dimension_scores      Raw scores from the GS for this turn.
        verdict               Final synthesised verdict for this turn.
        interception_point    The interception point role (user_prompt, tool_call, …).
        increment_turn        True only for user_prompt — one conversational turn =
                              one user message.  tool_call, agent_to_agent, and
                              llm_response evaluations contribute scores but must not
                              advance the human-readable turn counter.
        high_risk_policy_ids  IDs of severity-3 policies that fired COERCIVE this
                              turn.  Provided by the orchestrator after chain execution.
        """
        # Merge dimension scores — user_prompt only (worst-case accumulation).
        #
        # Session-level dimension tracking is exclusively concerned with USER
        # behaviour patterns (prompt safety, injection attempts, policy violations
        # in the user's own messages).  It intentionally mirrors the trust_score
        # delta logic below: only user_prompt turns can raise the session risk
        # profile or trigger the session_risk_scorer pack.
        #
        # agent_to_agent, tool_call, and llm_response violations are tracked at
        # the APP layer (AppTrustTracker) where they belong.  Merging them here
        # would cause sanctions hits on A2A/tool evals to inflate session
        # dimension_scores, making session_verdict escalate to COERCIVE even
        # though the user did nothing malicious (e.g. a legitimate transfer
        # re-submitted after an approval step).
        if interception_point == "user_prompt":
            for dim, score in dimension_scores.items():
                existing = self.dimension_scores.get(dim)
                if existing is None or score > existing:
                    self.dimension_scores[dim] = score

        self.turn_verdicts.append(verdict)
        if increment_turn:
            self.turn_count += 1

        # Trust score delta — ONLY user_prompt turns affect session trust.
        # llm_response / agent_to_agent / tool_call violations are audit-only:
        # they downgrade the per-turn verdict but must not decay session trust
        # (which would cause the session_risk_scorer to fire on the next
        # user_prompt and produce a spurious session-block).
        if interception_point == "user_prompt":
            # Self-heal a boot-time policy-load miss: if the import-time load fell back
            # to defaults (DB not ready), retry now that the DB is certainly up. Costs
            # one read only until the first success flips the flag.
            if not _POLICY_LOADED_FROM_DB:
                load_session_policy_from_db()
            delta = _SESSION_TRUST_DELTA.get(verdict.upper(), 0.0)
            self.trust_score = round(
                max(_SESSION_TRUST_FLOOR,
                    min(_SESSION_TRUST_CEILING, self.trust_score + delta)),
                4,
            )
            # Track minimum trust score reached (written to SessionSummary)
            if self.trust_score < self.min_trust_score:
                self.min_trust_score = self.trust_score

        # Coercive breakdown tracking
        if verdict.upper() == "COERCIVE":
            key = interception_point if interception_point in _INTERCEPTION_POINTS else "unknown"
            self.coercive_by_interception[key] = (
                self.coercive_by_interception.get(key, 0) + 1
            )
            for dim in dimension_scores:
                if dimension_scores[dim] >= 4:  # any non-permissive score that drove COERCIVE
                    self.coercive_by_policy[dim] = (
                        self.coercive_by_policy.get(dim, 0) + 1
                    )

        # High-risk policy recording (severity-3 policies that fired COERCIVE)
        if high_risk_policy_ids:
            for pid in high_risk_policy_ids:
                if pid not in self.high_risk_policies:
                    self.high_risk_policies.append(pid)

        self.touch()

    def is_expired(self, ttl_seconds: float) -> bool:
        return (time.time() - self.last_activity) > ttl_seconds

    # ── Emergent-risk helpers (Phase 1) ──────────────────────────────────────
    def is_frozen(self) -> bool:
        """True if the circuit-breaker freeze is active and not expired."""
        if not self.freeze:
            return False
        if self.freeze_ttl and (time.time() - self.freeze_at) > self.freeze_ttl:
            return False  # expired — treat as cleared (caller may lazily reset)
        return True

    def record_payment(self, amount: float, recipient: str) -> None:
        """Append a payment to the bounded structuring window (the gate's digest)."""
        self.payment_events.append(
            {"amount": float(amount or 0.0), "recipient": str(recipient or ""), "ts": time.time()}
        )
        if len(self.payment_events) > _PAYMENT_WINDOW:
            self.payment_events = self.payment_events[-_PAYMENT_WINDOW:]
        self.touch()

    def assess_structuring(self) -> Tuple[str, str]:
        """
        Deterministic, deliberately OVER-sensitive structuring assessment over the
        payment window (the cheap 'gate's eyes' digest — no LLM). Returns
        (verdict, reason):
          CONFIRMED = high-confidence structuring (safe to act now / HITL)
          WATCH     = suspicious; defer to the agentic layer for confirmation
          CLEAR     = nothing
        Looks for multiple payments that hug the just-under-report band, concentrated
        on one recipient, within the time window.
        """
        now    = time.time()
        recent = [p for p in self.payment_events if now - p["ts"] <= _STRUCT_WINDOW_S]
        band   = [p for p in recent if _STRUCT_BAND_LO <= p["amount"] < _STRUCT_REPORT_LIMIT]
        if not band:
            return "CLEAR", ""
        by_rcpt: Dict[str, int]   = {}
        sum_rcpt: Dict[str, float] = {}
        for p in band:
            r = p["recipient"] or "<unknown>"
            by_rcpt[r]  = by_rcpt.get(r, 0) + 1
            sum_rcpt[r] = sum_rcpt.get(r, 0.0) + p["amount"]
        top_r = max(by_rcpt, key=by_rcpt.get)
        n, tot = by_rcpt[top_r], sum_rcpt[top_r]
        if n >= _STRUCT_CONFIRM_N:
            return "CONFIRMED", (
                f"{n} payments of ${_STRUCT_BAND_LO:,.0f}-${_STRUCT_REPORT_LIMIT:,.0f} to "
                f"'{top_r}' within {int(_STRUCT_WINDOW_S)}s (total ${tot:,.0f}) — structuring "
                f"pattern (clustered just under the ${_STRUCT_REPORT_LIMIT:,.0f} report line)."
            )
        if n >= _STRUCT_WATCH_N:
            return "WATCH", (
                f"{n} payments just under ${_STRUCT_REPORT_LIMIT:,.0f} to '{top_r}' "
                f"(total ${tot:,.0f}) — possible structuring; pending agentic confirmation."
            )
        return "CLEAR", ""

    def record_a2a_message(self, content: str) -> None:
        """Append an A2A message content to the bounded collusion window (gate's digest)."""
        self.collusion_events.append({"content": str(content or ""), "ts": time.time()})
        if len(self.collusion_events) > _COLLUSION_WINDOW:
            self.collusion_events = self.collusion_events[-_COLLUSION_WINDOW:]
        self.touch()

    def assess_collusion(self) -> Tuple[str, str]:
        """
        Deterministic, deliberately OVER-sensitive collusion / control-evasion
        assessment over the A2A window (cheap 'gate's eyes' — no LLM). Returns
        (verdict, reason).

        Signature: a DENIAL/refusal on the channel followed by one or more PUSHES
        — re-requests, rewordings, or split/override attempts of the same intent.
        That post-denial persistence is what distinguishes evasion from a normal
        one-shot delegation. The agentic detector confirms or clears.
        """
        msgs = [str(e.get("content", "")).lower() for e in self.collusion_events]
        if not msgs:
            return "CLEAR", ""
        deny_idx = next(
            (i for i, m in enumerate(msgs)
             if any(k in m for k in _COLLUSION_DENY_MARKERS)),
            -1,
        )
        if deny_idx < 0:
            return "CLEAR", ""
        pushes = sum(
            1 for m in msgs[deny_idx + 1:]
            if any(k in m for k in _COLLUSION_PUSH_MARKERS)
        )
        if pushes >= _COLLUSION_CONFIRM_N:
            return "CONFIRMED", (
                f"{pushes} re-attempt(s) to push the same intent past the control AFTER a "
                f"denial on the agent channel — collusion / control-evasion signature."
            )
        if pushes >= _COLLUSION_WATCH_N:
            return "WATCH", (
                f"{pushes} re-request after a denial on the agent channel — possible "
                f"control-evasion; pending agentic confirmation."
            )
        return "CLEAR", ""

    def record_conflict_message(self, content: str) -> None:
        """Append a council/deliberation message to the bounded conflict window."""
        self.conflict_events.append({"content": str(content or ""), "ts": time.time()})
        if len(self.conflict_events) > _CONFLICT_WINDOW:
            self.conflict_events = self.conflict_events[-_CONFLICT_WINDOW:]
        self.touch()

    def assess_conflict(self) -> Tuple[str, str]:
        """
        Deterministic 'gate's eyes' for council deadlock (async path only). Counts
        accrued council messages; once enough rounds have passed without the flow
        terminating, flag WATCH and defer to the agentic conflict detector to decide
        whether it is a genuine hung split needing human arbitration. Never returns
        CONFIRMED — the agent owns that call (conflict = arbitration, not containment).
        """
        n = len(self.conflict_events)
        if n >= _CONFLICT_WATCH_N:
            return "WATCH", (
                f"{n} council/deliberation messages accrued without resolution — "
                f"possible deadlock; pending agentic conflict adjudication."
            )
        return "CLEAR", ""

    def apply_risk_class(self, risk_class: str, verdict: str, reason: str) -> None:
        """
        Record a per-class standing verdict and recompute the worst-of rollup
        (risk_verdict / risk_reason). Keeps the single rollup the gate enforces in
        sync, while letting the async detector reason per class.
        """
        verdict = (verdict or "CLEAR").upper()
        self.risk_flags[risk_class]   = verdict
        self.risk_reasons[risk_class] = reason or ""

        # Worst-of rollup across all classes (ties resolve to the class just set,
        # so the freshest matching reason surfaces).
        worst_cls = risk_class
        worst_rank = _RISK_RANK.get(verdict, 0)
        for cls, v in self.risk_flags.items():
            if _RISK_RANK.get((v or "CLEAR").upper(), 0) > worst_rank:
                worst_rank = _RISK_RANK.get((v or "CLEAR").upper(), 0)
                worst_cls = cls

        self.risk_verdict = self.risk_flags.get(worst_cls, "CLEAR")
        if worst_rank <= 0:
            self.risk_reason = ""
        else:
            self.risk_reason = f"[{worst_cls}] {self.risk_reasons.get(worst_cls, '')}"
        self.touch()

    def peak_interception_point(self) -> str:
        """Interception point with the highest COERCIVE count."""
        if not any(self.coercive_by_interception.values()):
            return "none"
        return max(self.coercive_by_interception, key=self.coercive_by_interception.get)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SessionState":
        known = set(cls.__dataclass_fields__.keys())  # type: ignore[attr-defined]
        filtered = {k: v for k, v in d.items() if k in known}
        # Back-fill fields added after initial deployment (forward-compatibility)
        if "coercive_by_interception" not in filtered:
            filtered["coercive_by_interception"] = {p: 0 for p in _INTERCEPTION_POINTS}
        if "prompt_hashes" not in filtered:
            filtered["prompt_hashes"] = []
        if "prompt_embeddings" not in filtered:
            filtered["prompt_embeddings"] = []
        if "prompt_window" not in filtered:
            filtered["prompt_window"] = []
        if "risk_flags" not in filtered:
            filtered["risk_flags"] = {}
        if "risk_reasons" not in filtered:
            filtered["risk_reasons"] = {}
        if "collusion_events" not in filtered:
            filtered["collusion_events"] = []
        if "conflict_events" not in filtered:
            filtered["conflict_events"] = []
        if "fasttrack_done" not in filtered:
            filtered["fasttrack_done"] = []
        return cls(**filtered)


# ── Session summary (compacted record) ────────────────────────────────────────

@dataclass
class SessionSummary:
    """
    Immutable compacted record produced when a session ends.

    Written by the store's compact() method before the live Session row is
    deleted.  Stored permanently for cross-session queries.  Never updated.

    Cross-session queries run against this store exclusively — the live
    session store and audit store are not touched for analytics.
    """
    session_id:              str
    app_id:                  str
    started_at:              float
    ended_at:                float
    end_reason:              str          # ttl_expired | terminated | trust_floor_reached
    duration_seconds:        float

    turn_count:              int
    coercive_count:          int
    normative_count:         int
    permissive_count:        int

    initial_trust_score:     float
    final_trust_score:       float
    min_trust_score:         float

    high_risk_policies:      List[str]
    peak_interception_point: str
    coercive_by_interception: Dict[str, int]
    coercive_by_policy:       Dict[str, int]

    def to_dict(self) -> dict:
        return asdict(self)


# ── Abstract base ──────────────────────────────────────────────────────────────

class SessionStore(ABC):
    """
    Thread-safe session state store.

    All implementations must be safe for concurrent access from multiple
    PolicyOrchestrator threads within a single PE process.

    Compaction
    ----------
    compact(session, reason) is called automatically by purge_expired() and
    terminate().  Callers should not need to invoke it directly.  Implementations
    should write the SessionSummary to a durable location before deleting the
    live session row.
    """

    @abstractmethod
    def get(self, session_id: str, app_id: str) -> Optional[SessionState]:
        """Return session state, or None if not found / expired."""
        ...

    @abstractmethod
    def put(self, state: SessionState) -> None:
        """Persist (create or overwrite) session state."""
        ...

    @abstractmethod
    def delete(self, session_id: str, app_id: str) -> None:
        """Remove a session from the live store (without compacting)."""
        ...

    @abstractmethod
    def purge_expired(self, ttl_seconds: float) -> int:
        """
        Compact then remove all sessions idle longer than ttl_seconds.
        Returns the count of sessions compacted and removed.
        Called by the background sweep thread — not on the evaluation hot path.
        """
        ...

    @abstractmethod
    def terminate(self, session_id: str, app_id: str) -> None:
        """
        Explicitly end a session (e.g. user logout).
        Compacts the session then removes it from the live store.
        """
        ...

    @abstractmethod
    def get_summaries(self, app_id: str) -> List[SessionSummary]:
        """Return all compacted summaries for a given app (cross-session queries)."""
        ...

    @abstractmethod
    def _all_states(self, app_id: str) -> List[SessionState]:
        """
        Return every live (non-expired) SessionState for an app.

        Backs the risk-store-driven discovery used by the async emergent-risk
        detector (Phase 2). NOT on the evaluation hot path — called on the
        detector's own cadence (seconds), and the live set is TTL-bounded.
        """
        ...

    def list_flagged_sessions(self, app_id: str) -> List[SessionState]:
        """
        Live sessions the DETERMINISTIC gate has flagged (risk_verdict WATCH or
        CONFIRMED) and not already frozen.

        This is the correct discovery surface for the async detector: it is driven
        by the RISK STORE (the thing that flags), not by the audit firehose — so it
        sees sessions regardless of which evaluation path created them
        (/v1/evaluate instrumented OR /v1/app/evaluate headless).
        """
        out: List[SessionState] = []
        for st in self._all_states(app_id):
            if st.is_frozen():
                continue  # already contained — nothing to adjudicate
            if (st.risk_verdict or "CLEAR").upper() in ("WATCH", "CONFIRMED"):
                out.append(st)
        return out

    def get_or_create(self, session_id: str, app_id: str) -> SessionState:
        """Return existing session or create a fresh one."""
        state = self.get(session_id, app_id)
        if state is None:
            state = SessionState(session_id=session_id, app_id=app_id)
            self.put(state)
            logger.debug(f"[Session] Created session_id={session_id} app_id={app_id}")
        return state

    def accumulate_a2a(self, session_id: str, app_id: str, content: str, tool_name: str = ""):
        """
        Atomically append an A2A message and return (new_count, state).

        The append + count is a read-modify-write on shared session state; doing it
        under the store lock (RLock is reentrant, so get_or_create/put nest safely)
        guarantees no increments are lost when multiple agents are governed
        CONCURRENTLY — the per-flow A2A cap then trips deterministically.
        """
        with self._lock:
            state = self.get_or_create(session_id, app_id)
            state.a2a_messages.append(content or "")
            if tool_name:
                state.a2a_tool_counts[tool_name] = state.a2a_tool_counts.get(tool_name, 0) + 1
            self.put(state)
            return len(state.a2a_messages), dict(state.a2a_tool_counts), state

    # ── Emergent-risk accumulators + circuit-breaker (Phase 1) ────────────────
    def accumulate_payment(self, session_id: str, app_id: str,
                           amount: float, recipient: str = "") -> SessionState:
        """Atomically record a payment into the structuring window and return state."""
        with self._lock:
            state = self.get_or_create(session_id, app_id)
            state.record_payment(amount, recipient)
            self.put(state)
            return state

    def accumulate_collusion(self, session_id: str, app_id: str,
                             content: str) -> SessionState:
        """Atomically record an A2A message into the collusion window and return state."""
        with self._lock:
            state = self.get_or_create(session_id, app_id)
            state.record_a2a_message(content)
            self.put(state)
            return state

    def accumulate_conflict(self, session_id: str, app_id: str,
                            content: str) -> SessionState:
        """Atomically record a council message into the conflict window and return state."""
        with self._lock:
            state = self.get_or_create(session_id, app_id)
            state.record_conflict_message(content)
            self.put(state)
            return state

    def set_risk_verdict(self, session_id: str, app_id: str,
                         verdict: str, reason: str = "",
                         risk_class: str = "") -> SessionState:
        """
        Set the standing emergent-risk verdict for a session.

        With risk_class, records a PER-CLASS verdict and recomputes the worst-of
        rollup (apply_risk_class). Without it, sets the rollup directly (legacy
        callers / the async CLEAR write-back).
        """
        with self._lock:
            state = self.get_or_create(session_id, app_id)
            if risk_class:
                state.apply_risk_class(risk_class, verdict, reason)
            else:
                state.risk_verdict = verdict
                state.risk_reason  = reason
            self.put(state)
            return state

    def set_freeze(self, session_id: str, app_id: str, frozen: bool,
                   reason: str = "", ttl: float = 0.0) -> Optional[SessionState]:
        """
        Trip or clear the per-session circuit-breaker freeze. The PE gate reads this
        on every evaluation and BLOCKs while it is active (Part III #4). Async detectors
        (Phase 2) call this on a CONFIRMED runaway; resolution clears it.
        """
        with self._lock:
            state = self.get(session_id, app_id)
            if state is None:
                if not frozen:
                    return None
                state = self.get_or_create(session_id, app_id)
            state.freeze        = bool(frozen)
            state.freeze_reason = reason if frozen else ""
            state.freeze_at     = time.time() if frozen else 0.0
            state.freeze_ttl    = float(ttl) if frozen else 0.0
            self.put(state)
            return state

    # ── Compaction helper (shared logic) ──────────────────────────────────────

    @staticmethod
    def _build_summary(state: SessionState, end_reason: str) -> SessionSummary:
        """Build a SessionSummary from a live SessionState."""
        now = time.time()
        verdicts   = state.turn_verdicts
        coercive   = sum(1 for v in verdicts if v.upper() == "COERCIVE")
        normative  = sum(1 for v in verdicts if v.upper() == "NORMATIVE")
        permissive = sum(1 for v in verdicts if v.upper() == "PERMISSIVE")

        return SessionSummary(
            session_id=state.session_id,
            app_id=state.app_id,
            started_at=state.created_at,
            ended_at=now,
            end_reason=end_reason,
            duration_seconds=round(now - state.created_at, 1),
            turn_count=state.turn_count,
            coercive_count=coercive,
            normative_count=normative,
            permissive_count=permissive,
            initial_trust_score=1.0,
            final_trust_score=state.trust_score,
            min_trust_score=state.min_trust_score,
            high_risk_policies=list(state.high_risk_policies),
            peak_interception_point=state.peak_interception_point(),
            coercive_by_interception=dict(state.coercive_by_interception),
            coercive_by_policy=dict(state.coercive_by_policy),
        )


# ── In-memory implementation ───────────────────────────────────────────────────

class InMemorySessionStore(SessionStore):
    """
    In-process session store backed by plain dicts.

    Thread-safe via a per-store RLock.
    State is lost when the Policy Engine restarts — suitable for dev and
    single-turn test scenarios where session persistence is not required.
    Summaries are also in-memory (lost on restart) — use DiskSessionStore
    if summary persistence is needed.
    """

    def __init__(self, ttl_seconds: float = 1800.0):
        self._ttl        = ttl_seconds
        self._lock       = threading.RLock()
        self._data:      Dict[str, SessionState]   = {}
        self._summaries: Dict[str, List[SessionSummary]] = {}  # keyed by app_id

    @staticmethod
    def _key(session_id: str, app_id: str) -> str:
        return f"{app_id}:{session_id}"

    # ── Core interface ─────────────────────────────────────────────────────────

    def get(self, session_id: str, app_id: str) -> Optional[SessionState]:
        key = self._key(session_id, app_id)
        with self._lock:
            state = self._data.get(key)
            if state is None:
                return None
            if state.is_expired(self._ttl):
                self._compact_and_delete(key, state, "ttl_expired")
                return None
            return state

    def put(self, state: SessionState) -> None:
        key = self._key(state.session_id, state.app_id)
        with self._lock:
            self._data[key] = state

    def delete(self, session_id: str, app_id: str) -> None:
        key = self._key(session_id, app_id)
        with self._lock:
            self._data.pop(key, None)

    def terminate(self, session_id: str, app_id: str) -> None:
        key = self._key(session_id, app_id)
        with self._lock:
            state = self._data.get(key)
            if state:
                self._compact_and_delete(key, state, "terminated")

    def purge_expired(self, ttl_seconds: float) -> int:
        with self._lock:
            expired_keys = [
                k for k, v in self._data.items() if v.is_expired(ttl_seconds)
            ]
            for key in expired_keys:
                self._compact_and_delete(key, self._data[key], "ttl_expired")
        if expired_keys:
            logger.info(f"[Session] Purged {len(expired_keys)} expired session(s)")
        return len(expired_keys)

    def get_summaries(self, app_id: str) -> List[SessionSummary]:
        with self._lock:
            return list(self._summaries.get(app_id, []))

    def _all_states(self, app_id: str) -> List[SessionState]:
        with self._lock:
            return [
                s for s in self._data.values()
                if s.app_id == app_id and not s.is_expired(self._ttl)
            ]

    # ── Internal ───────────────────────────────────────────────────────────────

    def _compact_and_delete(
        self, key: str, state: SessionState, reason: str
    ) -> None:
        """Build summary, store it, remove live row. Must be called under lock."""
        summary = self._build_summary(state, reason)
        self._summaries.setdefault(state.app_id, []).append(summary)
        del self._data[key]
        logger.debug(
            f"[Session] Compacted session_id={state.session_id} "
            f"app_id={state.app_id} reason={reason} "
            f"trust={summary.final_trust_score:.2f} "
            f"coercive={summary.coercive_count}"
        )

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._data)


# ── Disk implementation ────────────────────────────────────────────────────────

class DiskSessionStore(SessionStore):
    """
    Session store that persists each session as a JSON file.

    Layout:
      <store_dir>/sessions/<app_id>/<session_id>.json
      <store_dir>/summaries/<app_id>/<session_id>.json   ← compacted summaries

    Survives PE restarts.  Each file write is atomic via a temp-file rename
    so a crash during write cannot corrupt an existing session.

    Not suitable for multi-process PE deployments (no cross-process locking).
    Safe for single-process concurrent access via a per-store RLock.
    """

    def __init__(self, store_dir: str, ttl_seconds: float = 1800.0):
        self._root        = Path(store_dir)
        self._sessions    = self._root / "sessions"
        self._summaries   = self._root / "summaries"
        self._sessions.mkdir(parents=True, exist_ok=True)
        self._summaries.mkdir(parents=True, exist_ok=True)
        self._ttl  = ttl_seconds
        self._lock = threading.RLock()
        logger.info(f"[Session] DiskSessionStore initialised at {self._root}")

    # ── Path helpers ───────────────────────────────────────────────────────────

    def _session_path(self, session_id: str, app_id: str) -> Path:
        return self._sessions / app_id / f"{session_id}.json"

    def _summary_path(self, session_id: str, app_id: str) -> Path:
        return self._summaries / app_id / f"{session_id}.json"

    # ── Core interface ─────────────────────────────────────────────────────────

    def get(self, session_id: str, app_id: str) -> Optional[SessionState]:
        p = self._session_path(session_id, app_id)
        with self._lock:
            if not p.exists():
                return None
            try:
                data  = json.loads(p.read_text(encoding="utf-8"))
                state = SessionState.from_dict(data)
            except Exception as exc:
                logger.error(f"[Session] Corrupt session file {p}: {exc} — discarding")
                p.unlink(missing_ok=True)
                return None
            if state.is_expired(self._ttl):
                self._compact_and_delete(p, state, "ttl_expired")
                return None
            return state

    def put(self, state: SessionState) -> None:
        p   = self._session_path(state.session_id, state.app_id)
        tmp = p.with_suffix(".tmp")
        with self._lock:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(state.to_dict(), ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(p)   # atomic on POSIX; near-atomic on Windows (same volume)

    def delete(self, session_id: str, app_id: str) -> None:
        p = self._session_path(session_id, app_id)
        with self._lock:
            p.unlink(missing_ok=True)

    def terminate(self, session_id: str, app_id: str) -> None:
        p = self._session_path(session_id, app_id)
        with self._lock:
            if not p.exists():
                return
            try:
                state = SessionState.from_dict(
                    json.loads(p.read_text(encoding="utf-8"))
                )
                self._compact_and_delete(p, state, "terminated")
            except Exception as exc:
                logger.error(f"[Session] Failed to terminate session {p}: {exc}")
                p.unlink(missing_ok=True)

    def purge_expired(self, ttl_seconds: float) -> int:
        count = 0
        with self._lock:
            for f in self._sessions.rglob("*.json"):
                try:
                    state = SessionState.from_dict(
                        json.loads(f.read_text(encoding="utf-8"))
                    )
                    if state.is_expired(ttl_seconds):
                        self._compact_and_delete(f, state, "ttl_expired")
                        count += 1
                except Exception:
                    f.unlink(missing_ok=True)
                    count += 1
        if count:
            logger.info(f"[Session] Purged {count} expired session(s) from disk")
        return count

    def get_summaries(self, app_id: str) -> List[SessionSummary]:
        app_dir = self._summaries / app_id
        if not app_dir.exists():
            return []
        summaries = []
        with self._lock:
            for f in app_dir.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    summaries.append(SessionSummary(**data))
                except Exception as exc:
                    logger.warning(f"[Session] Corrupt summary {f}: {exc} — skipping")
        return summaries

    def _all_states(self, app_id: str) -> List[SessionState]:
        app_dir = self._sessions / app_id
        if not app_dir.exists():
            return []
        out: List[SessionState] = []
        with self._lock:
            for f in app_dir.glob("*.json"):
                try:
                    st = SessionState.from_dict(json.loads(f.read_text(encoding="utf-8")))
                    if not st.is_expired(self._ttl):
                        out.append(st)
                except Exception:
                    continue
        return out

    # ── Internal ───────────────────────────────────────────────────────────────

    def _compact_and_delete(
        self, session_path: Path, state: SessionState, reason: str
    ) -> None:
        """Write summary to disk then remove live session file. Under lock."""
        summary     = self._build_summary(state, reason)
        summary_p   = self._summary_path(state.session_id, state.app_id)
        summary_tmp = summary_p.with_suffix(".tmp")
        try:
            summary_p.parent.mkdir(parents=True, exist_ok=True)
            summary_tmp.write_text(
                json.dumps(summary.to_dict(), ensure_ascii=False), encoding="utf-8"
            )
            summary_tmp.replace(summary_p)
        except Exception as exc:
            logger.error(
                f"[Session] Failed to write summary for {state.session_id}: {exc}"
            )
        session_path.unlink(missing_ok=True)
        logger.debug(
            f"[Session] Compacted session_id={state.session_id} "
            f"app_id={state.app_id} reason={reason} "
            f"trust={summary.final_trust_score:.2f} "
            f"coercive={summary.coercive_count}"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  SQLite session store (preferred — persists to shared PAP database)
# ══════════════════════════════════════════════════════════════════════════════

class SqliteSessionStore(SessionStore):
    """
    Session store backed by pe_sessions and pe_session_summaries tables in
    the shared PAP SQLite database.

    Advantages over DiskSessionStore:
      - Survives PE restarts (like DiskSessionStore)
      - No per-file I/O — single WAL-mode SQLite DB handles concurrent reads
      - PAP dashboard can query active sessions without an HTTP round-trip
      - Atomic TTL check + delete via SQL WHERE clause

    Thread-safe via a per-store RLock.
    """

    def __init__(self, ttl_seconds: float = 1800.0):
        from core.database import engine
        self._engine = engine
        self._ttl    = ttl_seconds
        self._lock   = threading.RLock()
        logger.info(f"[Session] SqliteSessionStore initialised — ttl={ttl_seconds}s")

    # ── Core interface ─────────────────────────────────────────────────────────

    def get(self, session_id: str, app_id: str) -> Optional[SessionState]:
        from sqlalchemy import text
        import time
        with self._lock:
            with self._engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT state_json, last_activity_ts "
                        "FROM pe_sessions "
                        "WHERE session_id = :sid AND app_id = :aid"
                    ),
                    {"sid": session_id, "aid": app_id},
                ).fetchone()

            if row is None:
                return None

            state_json, last_ts = row[0], row[1]

            # TTL check
            if (time.time() - float(last_ts)) > self._ttl:
                try:
                    state_data = state_json if isinstance(state_json, dict) else json.loads(state_json)
                    state = SessionState.from_dict(state_data)
                    self._compact_and_remove(state, "ttl_expired")
                except Exception:
                    self._remove_session_row(session_id, app_id)
                return None

            try:
                state_data = state_json if isinstance(state_json, dict) else json.loads(state_json)
                return SessionState.from_dict(state_data)
            except Exception as exc:
                logger.error(f"[Session] Corrupt session row {session_id}/{app_id}: {exc} — removing")
                self._remove_session_row(session_id, app_id)
                return None

    def put(self, state: SessionState) -> None:
        from sqlalchemy import text
        state_data = json.dumps(state.to_dict())
        with self._lock:
            with self._engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO pe_sessions "
                        "  (session_id, app_id, state_json, created_at_ts, last_activity_ts, updated_at) "
                        "VALUES (:sid, :aid, :state, :created, :last, datetime('now')) "
                        "ON CONFLICT(session_id, app_id) DO UPDATE SET "
                        "  state_json = :state, last_activity_ts = :last, updated_at = datetime('now')"
                    ),
                    {
                        "sid":     state.session_id,
                        "aid":     state.app_id,
                        "state":   state_data,
                        "created": state.created_at,
                        "last":    state.last_activity,
                    },
                )

    def delete(self, session_id: str, app_id: str) -> None:
        with self._lock:
            self._remove_session_row(session_id, app_id)

    def terminate(self, session_id: str, app_id: str) -> None:
        from sqlalchemy import text
        with self._lock:
            with self._engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT state_json FROM pe_sessions "
                        "WHERE session_id = :sid AND app_id = :aid"
                    ),
                    {"sid": session_id, "aid": app_id},
                ).fetchone()
            if row is None:
                return
            try:
                state_data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                state = SessionState.from_dict(state_data)
                self._compact_and_remove(state, "terminated")
            except Exception as exc:
                logger.error(f"[Session] Failed to terminate {session_id}/{app_id}: {exc}")
                self._remove_session_row(session_id, app_id)

    def purge_expired(self, ttl_seconds: float) -> int:
        from sqlalchemy import text
        import time
        cutoff = time.time() - ttl_seconds
        count = 0
        with self._lock:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT session_id, app_id, state_json "
                        "FROM pe_sessions WHERE last_activity_ts < :cutoff"
                    ),
                    {"cutoff": cutoff},
                ).fetchall()

            for row in rows:
                try:
                    state_data = row[2] if isinstance(row[2], dict) else json.loads(row[2])
                    state = SessionState.from_dict(state_data)
                    self._compact_and_remove(state, "ttl_expired")
                except Exception:
                    self._remove_session_row(row[0], row[1])
                count += 1

        if count:
            logger.info(f"[Session] Purged {count} expired session(s) from DB")
        return count

    def get_summaries(self, app_id: str) -> List[SessionSummary]:
        from sqlalchemy import text
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT summary_json FROM pe_session_summaries "
                    "WHERE app_id = :aid ORDER BY ended_at_ts DESC"
                ),
                {"aid": app_id},
            ).fetchall()
        summaries = []
        for row in rows:
            try:
                data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                summaries.append(SessionSummary(**data))
            except Exception as exc:
                logger.warning(f"[Session] Corrupt summary for {app_id}: {exc} — skipping")
        return summaries

    @property
    def active_count(self) -> int:
        from sqlalchemy import text
        with self._engine.connect() as conn:
            row = conn.execute(text("SELECT COUNT(*) FROM pe_sessions")).fetchone()
        return row[0] if row else 0

    def _all_states(self, app_id: str) -> List[SessionState]:
        from sqlalchemy import text
        import time
        out: List[SessionState] = []
        with self._lock:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT state_json, last_activity_ts "
                        "FROM pe_sessions WHERE app_id = :aid"
                    ),
                    {"aid": app_id},
                ).fetchall()
        for row in rows:
            try:
                if (time.time() - float(row[1])) > self._ttl:
                    continue  # expired — skip (lazy purge happens on get())
                data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                out.append(SessionState.from_dict(data))
            except Exception:
                continue
        return out

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _remove_session_row(self, session_id: str, app_id: str) -> None:
        from sqlalchemy import text
        with self._engine.begin() as conn:
            conn.execute(
                text("DELETE FROM pe_sessions WHERE session_id = :sid AND app_id = :aid"),
                {"sid": session_id, "aid": app_id},
            )

    def _compact_and_remove(self, state: SessionState, reason: str) -> None:
        """Write summary to DB then remove live session row. Must be called under lock."""
        from sqlalchemy import text
        summary = self._build_summary(state, reason)
        summary_data = json.dumps(summary.to_dict())

        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO pe_session_summaries "
                    "  (session_id, app_id, summary_json, end_reason, turn_count, "
                    "   coercive_count, final_trust_score, started_at_ts, ended_at_ts, created_at) "
                    "VALUES (:sid, :aid, :data, :reason, :turns, :coercive, "
                    "        :trust, :started, :ended, datetime('now'))"
                ),
                {
                    "sid":      state.session_id,
                    "aid":      state.app_id,
                    "data":     summary_data,
                    "reason":   reason,
                    "turns":    summary.turn_count,
                    "coercive": summary.coercive_count,
                    "trust":    summary.final_trust_score,
                    "started":  summary.started_at,
                    "ended":    summary.ended_at,
                },
            )
            conn.execute(
                text("DELETE FROM pe_sessions WHERE session_id = :sid AND app_id = :aid"),
                {"sid": state.session_id, "aid": state.app_id},
            )

        logger.debug(
            f"[Session] Compacted session_id={state.session_id} app_id={state.app_id} "
            f"reason={reason} trust={summary.final_trust_score:.2f} coercive={summary.coercive_count}"
        )
