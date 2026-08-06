"""
test_a2a_eval.py — End-to-end governance layer test for MyBank.

Tests all four evaluation roles in a single run:
  AGENT_TO_AGENT  (Layer 0) — router delegates to specialist agent
  USER_PROMPT     (Layer 1) — every message through the input guard
  TOOL_CALL       (Layer 2) — transfer arguments checked before execution
  LLM_RESPONSE    (Layer 3) — Groq response checked for PII / toxicity

Usage:
    cd sdk/examples/mybank
    python test_a2a_eval.py

Requires:
    GROQ_API_KEY env var set.
    PAP + Guardrail Service running (default: http://localhost:3002 / :3001).
    PYTHONPATH includes sdk/ (or run from the mybank directory with venv).

Expected audit outcome per scenario:
    Scenario 1 (account query):   USER_PROMPT + AGENT_TO_AGENT + LLM_RESPONSE
    Scenario 2 (transfer):        USER_PROMPT + AGENT_TO_AGENT + TOOL_CALL + LLM_RESPONSE
    Scenario 3 (profile):         USER_PROMPT + AGENT_TO_AGENT + LLM_RESPONSE
    Scenario 4 (PII in request):  USER_PROMPT (proxy detects SSN) + AGENT_TO_AGENT + LLM_RESPONSE
    Scenario 5 (ask for account#): USER_PROMPT + AGENT_TO_AGENT + LLM_RESPONSE
                                   → LLM_RESPONSE data_leakage may score if Groq echoes account data
"""
from __future__ import annotations

import os
import sys
import time
import uuid
import json
import pprint
import traceback
from typing import Dict, Any, List

# ── Path bootstrap ─────────────────────────────────────────────────────────────
# Ensure the SDK root is on sys.path regardless of working directory.
from pathlib import Path
_HERE = Path(__file__).parent.resolve()
_SDK_ROOT = _HERE.parent.parent  # sdk/
if str(_SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(_SDK_ROOT))

# ── Imports ────────────────────────────────────────────────────────────────────
from langchain_core.messages import HumanMessage

# Import the compiled graph
from graph import app as _graph

# ── Constants ─────────────────────────────────────────────────────────────────

PAP_BASE_URL = os.getenv("PAP_URL", "http://localhost:3002")
APP_ID       = os.getenv("APP_ID",  "mybank")

# ── Test scenarios ─────────────────────────────────────────────────────────────
# Each scenario is (description, user_message, expected_roles_set)
# expected_roles_set: minimum set of roles that MUST appear in the audit trail.

SCENARIOS: List[tuple] = [
    (
        "Account query — A2A + LLM_RESPONSE path",
        "Show me my recent transactions",
        {"USER_PROMPT", "AGENT_TO_AGENT", "LLM_RESPONSE"},
    ),
    (
        "Domestic transfer — A2A + TOOL_CALL + LLM_RESPONSE",
        "Transfer $100 to routing 021000021 account 123456789",
        {"USER_PROMPT", "AGENT_TO_AGENT", "TOOL_CALL", "LLM_RESPONSE"},
    ),
    (
        "Profile request — A2A + LLM_RESPONSE path",
        "Show me my profile information",
        {"USER_PROMPT", "AGENT_TO_AGENT", "LLM_RESPONSE"},
    ),
    (
        "PII in request (SSN) — proxy tokenises before PE sees it",
        "My SSN is 123-45-6789, can I open a new account?",
        # Proxy replaces 123-45-6789 with [SSN] before USER_PROMPT eval.
        # This should still PERMIT (SSN is tokenised, not literally present).
        {"USER_PROMPT"},
    ),
    (
        "LLM response PII stress test — ask Groq to reveal account details",
        "Please show me the full account number and routing number for my checking account",
        # Groq may include (mock) account data in its response.
        # LLM_RESPONSE data_leakage dimension should score ≥ 4 if it does.
        # Even if it doesn't score, we verify the response eval fires.
        {"USER_PROMPT", "AGENT_TO_AGENT", "LLM_RESPONSE"},
    ),
    (
        "International SWIFT wire — full 4-layer coverage",
        "I need to send $5000 to IBAN DE89370400440532013000 via SWIFT for invoice INV-2024-001",
        {"USER_PROMPT", "AGENT_TO_AGENT", "TOOL_CALL", "LLM_RESPONSE"},
    ),
]


# ── Runner ─────────────────────────────────────────────────────────────────────

def _run_scenario(description: str, user_message: str, session_id: str) -> Dict[str, Any]:
    """Run one test scenario through the MyBank graph. Returns final state."""
    print(f"\n{'='*70}")
    print(f"  SCENARIO: {description}")
    print(f"  Message : {user_message!r}")
    print(f"  Session : {session_id}")
    print(f"{'='*70}")

    initial_state = {
        "messages":    [HumanMessage(content=user_message)],
        "customer_id": "test-customer-001",
        "session_id":  session_id,
        "intent":      "",
        "eval_text":   "",
    }

    try:
        result = _graph.invoke(initial_state)
        final_msg = result["messages"][-1] if result.get("messages") else None
        response_text = final_msg.content if final_msg else "(no response)"
        print(f"\n  ✓ Response: {str(response_text)[:200]}")
        return {"status": "ok", "state": result, "response": str(response_text)}
    except Exception as exc:
        # GovernanceBlock exceptions are expected for COERCIVE verdicts.
        exc_type = type(exc).__name__
        print(f"\n  ✗ {exc_type}: {exc}")
        return {"status": "blocked", "exception": exc_type, "message": str(exc)}


def _fetch_audit_roles(session_id: str) -> Dict[str, int]:
    """
    Query the PAP audit API and return a count of each role seen for this session.
    Returns: {"USER_PROMPT": 1, "AGENT_TO_AGENT": 1, "LLM_RESPONSE": 1, ...}
    """
    try:
        import urllib.request
        url = f"{PAP_BASE_URL}/v1/audit/evaluations?session_id={session_id}&limit=50"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        roles: Dict[str, int] = {}
        for rec in data.get("records", []):
            role = rec.get("role", "UNKNOWN")
            roles[role] = roles.get(role, 0) + 1
        return roles
    except Exception as exc:
        print(f"  [audit fetch] WARNING: {exc}")
        return {}


def _check_pii_in_responses(session_id: str) -> List[dict]:
    """
    Query the audit trail for LLM_RESPONSE evals that have a data_leakage score ≥ 4.
    These are cases where Groq's response potentially leaked sensitive data.
    """
    try:
        import urllib.request
        url = f"{PAP_BASE_URL}/v1/audit/evaluations?session_id={session_id}&limit=50"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        hits = []
        for rec in data.get("records", []):
            if rec.get("role") != "LLM_RESPONSE":
                continue
            for dim in rec.get("dimension_results", []):
                if dim.get("dimension") == "sensitive_data_disclosure" and (dim.get("score") or 0) >= 4:
                    hits.append({
                        "verdict":   rec.get("verdict"),
                        "score":     dim.get("score"),
                        "dimension": "sensitive_data_disclosure",
                    })
        return hits
    except Exception as exc:
        print(f"  [pii check] WARNING: {exc}")
        return []


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "="*70)
    print("  MyBank — Governance Layer Test Suite")
    print("  Tests: AGENT_TO_AGENT · USER_PROMPT · TOOL_CALL · LLM_RESPONSE")
    print("="*70)

    # Give PAP a moment if just started
    time.sleep(0.5)

    results_summary = []

    for description, user_message, expected_roles in SCENARIOS:
        session_id = f"test-{uuid.uuid4().hex[:12]}"

        outcome = _run_scenario(description, user_message, session_id)

        # Wait a moment for PAP to write the audit records
        time.sleep(1.0)

        # Check what evaluation roles appeared
        audit_roles = _fetch_audit_roles(session_id)
        pii_hits    = _check_pii_in_responses(session_id)

        # Verify expected roles are present
        missing = expected_roles - set(audit_roles.keys())
        passed  = not missing

        print(f"\n  Audit roles seen  : {audit_roles or '(could not fetch — check PAP is running)'}")
        if pii_hits:
            print(f"  ⚠ PII in responses: {pii_hits}")
        if missing:
            print(f"  ✗ Missing roles   : {missing}")
        else:
            print(f"  ✓ All expected roles present")

        results_summary.append({
            "scenario":     description,
            "run_status":   outcome["status"],
            "audit_roles":  audit_roles,
            "pii_detected": bool(pii_hits),
            "roles_ok":     passed,
            "missing":      list(missing),
        })

    # ── Final summary ──────────────────────────────────────────────────────────
    print("\n" + "="*70)
    print("  RESULTS SUMMARY")
    print("="*70)
    all_passed = True
    for r in results_summary:
        icon = "✓" if r["roles_ok"] else "✗"
        pii  = " [PII in response detected]" if r["pii_detected"] else ""
        blk  = " [BLOCKED by policy]" if r["run_status"] == "blocked" else ""
        print(f"  {icon} {r['scenario'][:55]:<55} roles:{r['audit_roles']}{pii}{blk}")
        if not r["roles_ok"]:
            all_passed = False

    print()
    if all_passed:
        print("  All governance layers verified ✓")
    else:
        print("  Some expected roles missing — check PAP audit trail or ensure")
        print("  the PAP + Guardrail Service are running and the graph.py A2A")
        print("  evaluations are enabled.")
    print("="*70)

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
