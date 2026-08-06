"""
rai-sdk-langgraph
─────────────────
Single-import distribution of the RAI Trust SDK for LangGraph applications.

Usage
─────
::

    pip install rai-sdk-langgraph

    from rai_sdk_langgraph import instrument, EnforcementMode

    app = instrument(
        app,
        base_url="http://localhost:3000",
        app_id="mybank",
        enforcement_mode=EnforcementMode.REDACT,
        otel_endpoint="http://localhost:4317",
    )

Everything needed is available from this single namespace — no sub-module
imports required.
"""

# ── Core instrumentation ──────────────────────────────────────────────────────
from rai_trust_sdk.langgraph.middleware import instrument
from rai_trust_sdk.langgraph.guard import LangGraphGuard

# ── Enforcement and evaluation control ───────────────────────────────────────
from rai_trust_sdk.types import (
    EnforcementMode,
    EvalMode,
    ContentRole,
    Verdict,
)

# ── Result types ──────────────────────────────────────────────────────────────
from rai_trust_sdk.types import (
    GovernanceResult,
    GovernanceBlock,
    HITLInterrupt,
    FailedEvaluation,
    PolicyResult,
    SessionSummary,
)

# ── Client (for advanced / deep-embedded use) ─────────────────────────────────
from rai_trust_sdk.client import TrustClient
from rai_trust_sdk.identity import AgentIdentity, MockIdentity

__version__ = "0.1.0"

__all__ = [
    # Instrumentation
    "instrument",
    "LangGraphGuard",
    # Enforcement
    "EnforcementMode",
    "EvalMode",
    "ContentRole",
    "Verdict",
    # Result types
    "GovernanceResult",
    "GovernanceBlock",
    "HITLInterrupt",
    "FailedEvaluation",
    "PolicyResult",
    "SessionSummary",
    # Client
    "TrustClient",
    "AgentIdentity",
    "MockIdentity",
]
