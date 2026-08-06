"""
Coordinator Agent — Pydantic schemas.

CoordinatorRequest  : input to POST /coordinate (called by Sentinel or PAP)
Intervention        : one typed intervention item (tiered, ITSM-annotated)
CoordinatorReport   : full output of the coordination pipeline
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Input ─────────────────────────────────────────────────────────────────────

class CoordinatorRequest(BaseModel):
    """
    Trigger a coordination cycle for a session.

    Sent by:
      - The PAP dashboard (on-demand "Coordinate" button)
      - The Sentinel Agent (auto-escalation when verdict == ALERT)
    """
    session_id:     str
    app_id:         Optional[str]   = None

    # Sentinel verdict context — caller can pass inline to skip the Sentinel pull
    sentinel_verdict:  Optional[str] = None   # ALERT | WATCH | CLEAR
    sentinel_checks:   List[str]     = Field(default_factory=list)
    sentinel_reasoning: str          = ""

    # Behavioral classification from the Sentinel LLM judge
    # (None when Sentinel ran heuristic-only — Coordinator infers from checks)
    pattern_type:      Optional[str]   = None
    rai_pillar:        Optional[str]   = None
    rai_dimensions:    List[str]       = Field(default_factory=list)
    agents_implicated: List[str]       = Field(default_factory=list)
    confidence:        Optional[float] = None
    evidence_turns:    List[int]       = Field(default_factory=list)


# ── Intervention (replaces Recommendation) ────────────────────────────────────

class Intervention(BaseModel):
    """
    A single typed intervention with tier classification and ITSM annotations.

    Tiers
    ─────
    1 — HITL (Human-in-the-Loop)
        Immediate operator / supervisor action.  Session suspension, account lock.
        ServiceNow: INCIDENT (urgency 1–2).

    2 — ALERT_NOTIFY (Alert & Notify)
        Automated notification to stakeholder teams.  SAR filing, audit logging.
        ServiceNow: INCIDENT (urgency 3) or PROBLEM.

    3 — POLICY_CHANGE (Policy Change Request)
        Modify detection thresholds, governance rules, or evaluation parameters.
        ServiceNow: CHANGE_REQUEST (Normal).

    4 — AGENT_CHANGE (Agent Change)
        Revise agent system prompts, permitted-tool scope, or ML model parameters.
        ServiceNow: CHANGE_REQUEST (Standard) + CMDB update.
    """
    tier:          int             # 1 | 2 | 3 | 4
    tier_label:    str             # HITL | ALERT_NOTIFY | POLICY_CHANGE | AGENT_CHANGE
    priority:      str             # HIGH | MEDIUM | LOW
    action:        str             # Imperative phrase ≤ 100 chars
    rationale:     str             # One sentence naming the specific pattern / check
    target:        str             # OPERATOR | COMPLIANCE | SYSTEM | AGENT_TEAM | ML_OPS
    itsm_artifact: str             = "NONE"  # INCIDENT | CHANGE_REQUEST | PROBLEM | NONE
    itsm_fields:   Dict[str, Any]  = Field(default_factory=dict)


# ── Output ────────────────────────────────────────────────────────────────────

class CoordinatorReport(BaseModel):
    """Full output of one coordination cycle."""
    session_id:        str
    app_id:            Optional[str]      = None
    sentinel_verdict:  str                = "CLEAR"
    overall_risk:      str                = "LOW"     # HIGH | MEDIUM | LOW

    # Behavioral pattern context (forwarded from Sentinel or inferred by Coordinator)
    pattern_type:      Optional[str]      = None
    rai_pillar:        Optional[str]      = None

    # Typed intervention catalog
    interventions:     List[Intervention] = Field(default_factory=list)

    summary:           str                = ""        # 1-2 sentence synthesis
    llm_used:          bool               = False
    coordinated_at:    str                = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    )
    source_interaction: str               = ""        # Sentinel interaction_id that prompted this


# ── HTTP responses ─────────────────────────────────────────────────────────────

class ReportResponse(BaseModel):
    """Response body for GET /reports/{session_id}."""
    session_id: str
    found:      bool
    report:     Optional[CoordinatorReport] = None
