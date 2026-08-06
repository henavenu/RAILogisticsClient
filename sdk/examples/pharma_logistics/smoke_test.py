"""
MyBank Smoke Test — Non-Interactive
====================================
Runs 4 scenarios against the instrumented banking graph and then
verifies that Tier 1–3 metrics recorded the traffic in PAP.

Usage (from mybank_clean directory):
    python smoke_test.py

Prerequisites:
    - PAP running on port 3002
    - PE running on port 3000
    - GROQ_API_KEY set in .env

Scenarios:
    1. Balance check          (account node, USER_PROMPT + LLM_RESPONSE)
    2. Domestic transfer      (transfer_router → domestic_agent, TOOL_CALL)
    3. International transfer (transfer_router → intl_wire_agent, A2A)
    4. Iran transfer          (should be BLOCKED by sanctions)
"""
from __future__ import annotations

import os
import sys
import uuid
import json
import logging
from pathlib import Path
from typing import Optional

# ── Path setup ────────────────────────────────────────────────────────────────
_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_dir))

from dotenv import load_dotenv
load_dotenv(_dir / ".env")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpcore._async").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logger = logging.getLogger("smoke_test")

# ── Imports ───────────────────────────────────────────────────────────────────
from langchain_core.messages import AIMessage, HumanMessage

from graph import app as _raw_graph
from rai_sdk_langgraph import instrument, EnforcementMode
from rai_privacy_proxy import PrivacyProxy, ProxyConfig

# ── Instrument graph (mirrors server.py exactly) ──────────────────────────────
_proxy = PrivacyProxy(ProxyConfig(
    project_names=["Project Titan", "Project Aurora", "Operation Phoenix"],
    client_names=[],
))

banking_graph = instrument(
    _raw_graph,
    base_url=os.getenv("TRUST_PE_URL", "http://localhost:3000"),
    app_id=os.getenv("TRUST_APP_ID", "mybank"),
    enforcement_mode=EnforcementMode(os.getenv("TRUST_ENFORCEMENT_MODE", EnforcementMode.REDACT)),
    agent_nodes=["intl_wire_agent"],
    a2a_tools={"international_transfer": "intl_wire_agent"},
    otel_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
    otel_protocol=os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc"),
    privacy_proxy=_proxy,
)

SESSION_ID = f"smoke-test-{uuid.uuid4().hex[:8]}"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _run(message: str, state: Optional[dict] = None) -> dict:
    """Invoke the banking graph with one message, return the result state."""
    if state is None:
        state = {
            "messages":     [],
            "customer_id":  "cust_001",
            "session_id":   SESSION_ID,
            "intent":       "",
            "transfer_type": "",
        }

    state["messages"] = list(state["messages"]) + [HumanMessage(content=message)]

    try:
        result = banking_graph.invoke(state)
        return result
    except Exception as exc:
        logger.error("Graph invoke error: %s", exc)
        return {**state, "_error": str(exc)}


def _last_reply(result: dict) -> str:
    """Extract the last AI message text from a result state."""
    for msg in reversed(result.get("messages", [])):
        if isinstance(msg, AIMessage):
            c = msg.content
            return c if isinstance(c, str) else str(c)
    return result.get("_error", "(no response)")


def _check_pap(path: str, params: str = "window=7d") -> dict:
    """Fetch a PAP metric endpoint and return the JSON."""
    import urllib.request
    url = f"http://localhost:3002{path}?{params}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        return {"error": str(exc)}


# ── Scenarios ─────────────────────────────────────────────────────────────────

SEPARATOR = "=" * 70


def run_scenario(label: str, message: str, state: Optional[dict] = None) -> tuple[str, dict]:
    print(f"\n{SEPARATOR}")
    print(f"  SCENARIO: {label}")
    print(f"  Input   : {message}")
    print(SEPARATOR)
    result = _run(message, state)
    reply = _last_reply(result)
    short = reply[:300] + ("…" if len(reply) > 300 else "")
    print(f"  Reply   : {short}")
    return reply, result


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*70}")
    print(f"  MyBank Smoke Test -- Session: {SESSION_ID}")
    print(f"  PE: {os.getenv('TRUST_PE_URL', 'http://localhost:3000')}")
    print(f"  PAP: http://localhost:3002")
    print(f"{'='*70}")

    # 1. Balance check (account node)
    run_scenario("Balance Check", "What is my current balance?")

    # 2. Domestic transfer (domestic_agent node)
    run_scenario(
        "Domestic ACH Transfer",
        "Transfer $150 from checking to routing number 021000021, account 9876543210, memo: rent",
    )

    # 3. International wire (intl_wire_agent / A2A node)
    run_scenario(
        "International Wire (Germany — should PASS)",
        "Send 500 USD to Hans Mueller, IBAN DE89370400440532013000, SWIFT COBADEFFXXX, Germany, purpose: consulting fee, from checking",
    )

    # 4. Sanctioned country transfer (should be blocked)
    run_scenario(
        "Sanctioned Transfer (Iran — should BLOCK)",
        "Send $9500 to Ahmad Karimi at Bank Melli Iran, account 1234567890, SWIFT MELIIRTHXXX, Iran, from checking",
    )

    # ── Metric verification ────────────────────────────────────────────────────
    print(f"\n{SEPARATOR}")
    print("  METRIC VERIFICATION (PAP)")
    print(SEPARATOR)

    # Tier 1
    t1 = _check_pap("/v1/metrics/tier1")
    if "error" in t1:
        print(f"  Tier 1 : ERROR - {t1['error']}")
    else:
        metrics = t1.get("metrics", {})
        vr = metrics.get("violation_rate", {})
        a2a = metrics.get("a2a_safety_score", {})
        print(f"  Tier 1 : computed_from={t1.get('computed_from',0)}  "
              f"violation_rate={vr.get('flag_rate_pct','?')}%  "
              f"roles={t1.get('role_breakdown',{})}")
        print(f"  A2A    : avg={a2a.get('avg','?')}  "
              f"sample={a2a.get('sample','?')}  "
              f"status={a2a.get('status','?')}")

    # Tier 2
    t2 = _check_pap("/v1/metrics/tier2")
    if "error" in t2:
        print(f"  Tier 2 : ERROR - {t2['error']}")
    else:
        dims = t2.get("dimensions", [])
        print(f"  Tier 2 : synced_rows={t2.get('synced_rows',0)}  dimensions={len(dims)}")
        for d in dims[:5]:
            print(f"    [{d['eval_name']:32}] pass={d['pass_count']:3}/{d['total']:3}  "
                  f"avg={d['avg_score']}")

    # Trust Score
    ts = _check_pap("/v1/trust-score")
    if "error" in ts:
        print(f"  Trust  : ERROR - {ts['error']}")
    else:
        plat = ts.get("platform", {})
        print(f"  Trust  : score={plat.get('trust_score','?')}  "
              f"status={plat.get('status','?')}  "
              f"evals={plat.get('total_evals','?')}")

    # COGS (prefix is /v1/cogs, not /v1/metrics/cogs)
    cogs = _check_pap("/v1/cogs")
    if "error" in cogs:
        print(f"  COGS   : ERROR - {cogs['error']}")
    else:
        summary = cogs.get("summary", {})
        models = cogs.get("by_model", [])
        total = summary.get("total_cost_usd", 0)
        model_names = [m.get("model", m.get("model_id", "?")) for m in models]
        print(f"  COGS   : total_cost=${total:.6f}  "
              f"interactions={summary.get('total_interactions',0)}  "
              f"models={model_names}")

    print(f"\n{SEPARATOR}")
    print("  Smoke test complete.")
    print(SEPARATOR)
    print()


if __name__ == "__main__":
    main()
