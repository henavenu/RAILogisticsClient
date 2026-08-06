"""
Sentinel Agent — Pydantic schemas.

WebhookPayload   : body of the PE's POST /evaluate webhook
FiredCheck       : one pre-check metric that exceeded its threshold
SentinelVerdict  : output of the two-pass analysis pipeline
VerdictResponse  : HTTP response for GET /verdicts/{session_id}
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, Field


# ── Webhook payload (PE → Sentinel) ──────────────────────────────────────────

class WebhookPayload(BaseModel):
    """
    Forwarded by SentinelWebhookForwarder in policy-engine/audit/sentinel_webhook.py.
    Mirrors the fields emitted there — must stay in sync.
    """
    interaction_id: str
    session_id:     Optional[str]   = None
    app_id:         Optional[str]   = None
    role:           str             = ""
    verdict:        str             = "PERMISSIVE"
    violations:     List[str]       = Field(default_factory=list)
    turn_number:    Optional[int]   = None
    trigger_reason: str             = "N_TURN"   # "COERCIVE" | "N_TURN"
    fired_at:       Optional[str]   = None


# ── Pre-check result ──────────────────────────────────────────────────────────

class FiredCheck(BaseModel):
    name:        str
    value:       float
    threshold:   float
    description: str = ""


# ── Sentinel verdict ──────────────────────────────────────────────────────────

class SentinelVerdict(BaseModel):
    """
    Output of the SentinelEvaluator two-pass pipeline.
    Stored in VerdictStore keyed by session_id.

    Two observation planes:
      • RAI Pillars / Dimensions  — per-turn signals from the Policy Engine,
        surfaced here as rai_pillar and rai_dimensions.
      • Behavioral Dimensions     — cross-turn patterns detected by this Sentinel,
        surfaced here as pattern_type.
    """
    # ── Core verdict ──────────────────────────────────────────────────────────
    session_id:      str
    app_id:          Optional[str]       = None
    verdict:         str                 = "CLEAR"   # ALERT | WATCH | CLEAR
    trigger_reason:  str                 = "N_TURN"
    fired_checks:    List[FiredCheck]    = Field(default_factory=list)
    llm_used:        bool                = False
    llm_reasoning:   str                 = ""
    evaluated_at:    str                 = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    )
    interaction_id:  str                 = ""   # the PE interaction that triggered this
    event_count:     int                 = 0    # events pulled from PE for this analysis
    pull_failed:     bool                = False

    # ── Behavioral classification (populated by LLM judge) ───────────────────
    # pattern_type: the dominant cross-turn behavioral pattern detected.
    # Valid values (from policy.yaml taxonomy):
    #   enumeration | structuring | dimensional_escalation | role_escalation |
    #   scope_violation | agent_crossover | collusion | credential_harvesting |
    #   prompt_injection | persistence | none
    pattern_type:      Optional[str]    = None

    # rai_pillar: the primary RAI governance pillar most affected.
    # Valid values: Safety | Fairness | Transparency | Accountability | Privacy | Security
    rai_pillar:        Optional[str]    = None

    # rai_dimensions: the specific Policy Engine evaluation dimensions that fired
    # across turns (e.g. ["data_leakage", "sanctions_evasion_signal"]).
    rai_dimensions:    List[str]        = Field(default_factory=list)

    # agents_implicated: agent names that appeared in violation events.
    agents_implicated: List[str]        = Field(default_factory=list)

    # confidence: LLM confidence in the pattern classification (0.0 – 1.0).
    confidence:        Optional[float]  = None

    # evidence_turns: turn numbers that carry the key violation events.
    evidence_turns:    List[int]        = Field(default_factory=list)


# ── Sync evaluation (PE → Sentinel inline, PE-initiated) ─────────────────────

class SyncEvalRequest(BaseModel):
    """
    Body for POST /evaluate-sync.

    Called synchronously by the Policy Engine when accumulated agent_to_agent
    messages reach the configured cap (A2A_HITL_THRESHOLD, default 6 messages =
    3 full back-and-forth cycles).

    Completely decoupled from the async observer path (POST /evaluate):
      Sync mode    → PE-initiated, Sentinel acts as pure evaluator, no Trust Radar post.
      Async mode   → Fire-and-forget webhook, Trust Radar posting as usual.
    """
    session_id:       str
    a2a_messages:     List[str]          = Field(default_factory=list)
    # POST-MVP: review whether proposed_options evaluation belongs in Sentinel or a
    # dedicated evaluator service.  Built now to unblock the myBank demo — Sentinel
    # picks from structured card options supplied by the application agents when a
    # NORMATIVE verdict is reached, surfacing the recommendation in the HITL payload
    # for the approver.
    proposed_options: Optional[List[dict]] = None


class SyncEvalResponse(BaseModel):
    """
    Response body for POST /evaluate-sync.

    verdict            PERMISSIVE — no conflict detected, execution may continue.
                       NORMATIVE  — conflict or deadlock detected; PE will queue a
                                   REQUIRE_HUMAN_REVIEW directive.
    pattern_type       Behavioural pattern label (e.g. "agent_conflict",
                       "cross_border_coordination") — None when PERMISSIVE.
    reasoning          One or two sentence LLM explanation — empty string on fallback.
    recommended_option POST-MVP: Sentinel's pick from proposed_options when NORMATIVE
                       and options were supplied.  None otherwise.
    """
    verdict:            str                  = "PERMISSIVE"  # PERMISSIVE | NORMATIVE
    pattern_type:       Optional[str]        = None
    reasoning:          str                  = ""
    recommended_option: Optional[dict]       = None          # POST-MVP: Sentinel's pick


# ── HTTP response shapes ──────────────────────────────────────────────────────

class EvaluateAck(BaseModel):
    """Immediate acknowledgement returned to the PE webhook caller."""
    status:         str = "accepted"
    interaction_id: str
    session_id:     Optional[str] = None


class VerdictResponse(BaseModel):
    """Response body for GET /verdicts/{session_id}."""
    session_id: str
    found:      bool
    verdict:    Optional[SentinelVerdict] = None
