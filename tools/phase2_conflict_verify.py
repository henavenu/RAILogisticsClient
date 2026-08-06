#!/usr/bin/env python3
"""Phase 2c verification (ASYNC conflict mode): with PE_A2A_SYNC_SENTINEL=false the PE
hands deadlock to the async loop. Drive a hung council transcript (sanctioned council
tool), confirm the deterministic gate flags `conflict` WATCH, then confirm the Sentinel
loop adjudicates the deadlock and raises HITL via the directive queue (NOT a freeze).

Requires:  PE started with PE_A2A_SYNC_SENTINEL=false
           Sentinel started with SENTINEL_DETECTORS=structuring,collusion,conflict
"""
import json, os, time, urllib.request

PE  = os.environ.get("PE_URL", "http://127.0.0.1:3003")
APP = "mybank"
SID = f"phase2-conflict-{int(time.time())}"

# A hung 2-2 council split that repeats across rounds (no convergence).
COUNCIL = [
    "Round 1 — TxnMonitoring: UPHOLD the denial (risk indicators present).",
    "Round 1 — Revenue: OVERTURN (customer lifetime value is high).",
    "Round 1 — CX: OVERTURN (false-decline harms the relationship).",
    "Round 1 — RiskPolicy: UPHOLD (geo + amount breach policy).",
    "Round 2 — TxnMonitoring: UPHOLD, position unchanged.",
    "Round 2 — Revenue: OVERTURN, position unchanged.",
    "Round 2 — CX: OVERTURN, position unchanged.",
    "Round 2 — RiskPolicy: UPHOLD, position unchanged. 2-2 split persists.",
]


def post(path, body):
    r = urllib.request.Request(PE + path, data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read().decode())


def get(path):
    with urllib.request.urlopen(PE + path, timeout=15) as resp:
        return json.loads(resp.read().decode())


def council(content, n):
    out = post("/v1/app/evaluate", {
        "app_id": APP, "request_id": f"{SID}-c{n}", "role": "agent_to_agent",
        "content": content, "session_id": SID, "eval_mode": "SYNC",
        "metadata": {"tool_name": "request_policy_review"},
    })
    return out.get("verdict")


print(f"=== Phase 2c async-conflict verification (session={SID}) ===\n")
print("[1] Drive a hung council transcript (sanctioned tool=request_policy_review)")
for i, msg in enumerate(COUNCIL, 1):
    v = council(msg, i)
    print(f"    msg {i}: verdict={v}  | {msg[:60]}")
risk = get(f"/internal/session-risk/{SID}?app_id={APP}")
print(f"    deterministic risk_flags = {risk.get('risk_flags')}  rollup={risk.get('risk_verdict')}")
conflict_flagged = str(risk.get("risk_flags", {}).get("conflict", "")).upper() in ("WATCH", "CONFIRMED")
if not conflict_flagged:
    print("    !! conflict not flagged — is PE_A2A_SYNC_SENTINEL=false? (sync mode is a no-op here)")

print("\n[2] Wait for the Sentinel loop to adjudicate the deadlock & raise a HITL directive...")
hitl = False
for t in range(26):
    time.sleep(1)
    # The loop clears the conflict flag once it has adjudicated. Probe by draining the
    # session's pending directive with a benign follow-up evaluate and inspecting it.
    r = post("/v1/app/evaluate", {
        "app_id": APP, "request_id": f"{SID}-probe{t}", "role": "user_prompt",
        "content": "status check", "session_id": SID, "eval_mode": "SYNC",
    })
    directives = r.get("directives") or []
    if any(str(d.get("action", "")).lower() == "require_human_review" for d in directives):
        hitl = True
        d0 = next(d for d in directives if str(d.get("action", "")).lower() == "require_human_review")
        print(f"    t+{t}s: HITL raised by agent — category={d0.get('category')} reason={str(d0.get('reason',''))[:80]}")
        break
if not hitl:
    print("    (no HITL directive within timeout — check Sentinel loop / detector)")

print("\n=== RESULT ===")
print(f"  deterministic conflict flag raised : {'PASS' if conflict_flagged else 'FAIL'}")
print(f"  agent raised HITL (no freeze)      : {'PASS' if hitl else 'FAIL'}")
frozen = bool(get(f'/internal/session-risk/{SID}?app_id={APP}').get('frozen'))
print(f"  session NOT frozen (arbitration)   : {'PASS' if not frozen else 'FAIL'}")
print("  => PASS" if (conflict_flagged and hitl and not frozen) else "  => CHECK output")
