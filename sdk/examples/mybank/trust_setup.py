"""
MyBank — RAI Trust SDK Configuration
══════════════════════════════════════
Three governance layers protect this application:

  Layer 1 · Input Guard (USER_PROMPT)
    Every user request is evaluated before routing. Catches prompt
    injection and policy violations before any banking logic runs.

  Layer 2 · Transfer Pre-Execution (TOOL_CALL)
    Transfer arguments are evaluated before funds move. Catches
    sanctions hits, AML structuring, and anomalous wire patterns.

  Layer 3 · Response Guard (LLM_RESPONSE)
    Every LLM-generated response is evaluated before delivery.
    Catches PII leakage and hallucinated account data.
"""
from __future__ import annotations

import logging
import os

# SDK imports — sys.path configured in main.py before this module is loaded
from rai_trust_sdk import TrustClient
from rai_trust_sdk.identity import MockIdentity
from rai_trust_sdk.langgraph import LangGraphGuard
from rai_trust_sdk.privacy import PrivacyProxy, ProxyConfig
from rai_trust_sdk.types import ContentRole, EvalMode, GovernanceResult, Verdict

logger = logging.getLogger(__name__)

# ── Trust Client (singleton) ──────────────────────────────────────────────────

trust_client = TrustClient(
    base_url=os.environ.get("TRUST_PE_URL", "http://localhost:3000"),
    app_id="mybank",
    identity=MockIdentity(agent_id="mybank-agent-v1"),
    timeout=float(os.environ.get("TRUST_TIMEOUT", "5")),
)


# ── Governance result logger ──────────────────────────────────────────────────

def _log_governance_result(result: GovernanceResult, role: ContentRole) -> None:
    """
    Log a governance evaluation result with visual verdict indicators.

    Icons:
      ✅  PERMISSIVE — all checks passed, proceed normally
      ⚠️  NORMATIVE  — warnings raised, proceed with caution
      🚫  COERCIVE   — policy violation, execution blocked
    """
    verdict = result.verdict

    if verdict == Verdict.PERMISSIVE:
        icon = "✅"
        level = logging.INFO
    elif verdict == Verdict.NORMATIVE:
        icon = "⚠️ "
        level = logging.WARNING
    else:  # COERCIVE
        icon = "🚫"
        level = logging.ERROR

    # Console print for immediate demo visibility
    print(
        f"  {icon} [RAI Trust] role={role.value} verdict={verdict.value} "
        f"request_id={result.request_id}"
    )

    if result.violations:
        violations_str = " | ".join(result.violations)
        print(f"      Violations  : {violations_str}")

    if result.remediations:
        for rem in result.remediations:
            print(f"      Remediation : {rem}")

    if result.session_summary:
        ss = result.session_summary
        print(
            f"      Session     : turns={ss.turn_count} "
            f"composite={ss.composite_score:.2f} "
            f"worst={ss.worst_verdict}"
        )

    # Structured log for observability pipelines
    logger.log(
        level,
        "[RAI Trust] role=%s verdict=%s violations=%s request_id=%s",
        role.value,
        verdict.value,
        result.violations,
        result.request_id,
    )


# ── Privacy Proxy (singleton) ─────────────────────────────────────────────────
# Stage 1 runs entirely in-process — no network, no latency added.
# Routing numbers, account numbers, SSNs, emails, etc. are replaced with
# placeholder tokens ([ROUTING_NUMBER], [BANK_ACCOUNT], …) so that neither
# the Policy Engine nor the LLM ever sees the raw sensitive values.
# enable_classifier=False skips Stage 2 (sentence-transformers not required).

privacy_proxy = PrivacyProxy(ProxyConfig(
    domain_description=(
        "Retail banking AI assistant for account balances, "
        "transaction history, and domestic/international wire transfers."
    ),
    enable_classifier=False,
))


# ── Guard factory ─────────────────────────────────────────────────────────────

def make_session_guard(session_id: str) -> LangGraphGuard:
    """
    Create a LangGraphGuard bound to the given session.

    The guard uses SYNC eval_mode so a COERCIVE verdict raises GovernanceBlock
    immediately, terminating graph execution before any banking operation runs.
    The on_result hook prints governance outcomes to the console for demo
    visibility and forwards structured data to the application logger.
    """
    return LangGraphGuard(
        client=trust_client,
        session_id=session_id,
        eval_mode=EvalMode.SYNC,
        on_result=_log_governance_result,
    )
