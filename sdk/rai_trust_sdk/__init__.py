"""
RAI Trust SDK
─────────────
Governance for LangChain and LangGraph agents.

Quick start
───────────
::

    from rai_trust_sdk import TrustClient, GovernanceBlock
    from rai_trust_sdk.types import ContentRole, EvalMode
    from rai_trust_sdk.langchain import RaiTrustCallbackHandler
    from rai_trust_sdk.langgraph import LangGraphGuard

    # 1. Instantiate the client once per app
    client = TrustClient(base_url="http://pe:3000", app_id="finapp")

    # 2a. LangChain — attach as a callback (zero-touch for outer-edge eval)
    handler = RaiTrustCallbackHandler(client=client)
    chain.invoke({"input": "..."}, config={"callbacks": [handler]})

    # 2b. LangGraph — decorate individual nodes explicitly
    guard = LangGraphGuard(client=client)

    @guard.wrap(content_key="messages")
    def llm_node(state):
        ...

    # 3. OTEL (optional, provider-agnostic)
    from rai_trust_sdk.observability import setup_otel
    tracer = setup_otel("finapp-agent")                 # local Jaeger
    tracer = setup_otel(                                # Arize / any OTLP
        "finapp-agent",
        endpoint="https://otlp.arize.com:443",
        headers={"authorization": "Bearer <KEY>"},
        insecure=False,
    )
"""

from .client import TrustClient
from .identity import AgentIdentity, MockIdentity
from .types import (
    ContentRole,
    Directive,
    DirectiveAction,
    DirectiveSource,
    EnforcementMode,
    EvalMode,
    FailedEvaluation,
    GovernanceBlock,
    GovernanceResult,
    HITLCategory,
    HITLInterrupt,
    PolicyResult,
    SessionSummary,
    Verdict,
)

__all__ = [
    # Client
    "TrustClient",
    # Identity / NHI
    "AgentIdentity",
    "MockIdentity",
    # Core types
    "ContentRole",
    "EnforcementMode",
    "EvalMode",
    "Verdict",
    "GovernanceResult",
    "GovernanceBlock",
    "HITLInterrupt",
    "FailedEvaluation",
    "PolicyResult",
    "SessionSummary",
    # Directive taxonomy
    "Directive",
    "DirectiveAction",
    "DirectiveSource",
    "HITLCategory",
]

__version__ = "0.1.0"
