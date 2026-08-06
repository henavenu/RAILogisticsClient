#!/usr/bin/env python3
"""Phase 2 verification: deterministic flag -> AGENT autonomously trips the breaker -> block.
No test-set freeze: the Sentinel emergent loop must freeze the session on its own."""
import json, os, time, urllib.request

PE  = os.environ.get("PE_URL", "http://127.0.0.1:3003")
APP = "mybank"
SID = f"phase2-verify-{int(time.time())}"


def post(path, body):
    r = urllib.request.Request(PE + path, data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read().decode())


def get(path):
    with urllib.request.urlopen(PE + path, timeout=15) as resp:
        return json.loads(resp.read().decode())


def pay(amount, recipient, n):
    out = post("/v1/app/evaluate", {
        "app_id": APP, "request_id": f"{SID}-p{n}", "role": "user_prompt",
        "content": f"Please wire ${amount:,} to {recipient} for invoice {n}.",
        "session_id": SID, "eval_mode": "SYNC",
        "metadata": {"amount": amount, "recipient_id": recipient},
    })
    return out.get("verdict")


print(f"=== Phase 2 verification (session={SID}) ===\n")
print("[1] Post a structuring sequence (3x just-under, same recipient)")
for i, amt in enumerate([9800, 9950, 9700], 1):
    v = pay(amt, "OffshoreHoldings Ltd", i)
    print(f"    payment {i} (${amt:,}) -> verdict={v}")
risk = get(f"/internal/session-risk/{SID}?app_id={APP}")
print(f"    deterministic risk_verdict = {risk.get('risk_verdict')}  frozen={risk.get('frozen')}")

print("\n[2] Wait for the Sentinel emergent loop to adjudicate & trip the breaker (no test freeze)...")
frozen = False
for t in range(24):  # up to ~24s
    risk = get(f"/internal/session-risk/{SID}?app_id={APP}")
    if risk.get("frozen"):
        frozen = True
        print(f"    t+{t}s: AGENT froze the session — reason: {risk.get('freeze_reason','')[:90]}")
        break
    time.sleep(1)
if not frozen:
    print("    (no freeze within timeout — check Sentinel loop / LLM)")

print("\n[3] Post another action -> circuit-breaker should block it")
v_after = pay(500, "Normal Payee", 99) if frozen else None
print(f"    next action verdict = {v_after}  (expected COERCIVE)")

print("\n[4] Cleanup: clear the freeze")
post("/internal/session-freeze", {"session_id": SID, "app_id": APP, "frozen": False})

print("\n=== RESULT ===")
print(f"  agent autonomously tripped breaker : {'PASS' if frozen else 'FAIL'}")
print(f"  breaker then blocks next action    : {'PASS' if v_after == 'COERCIVE' else 'FAIL'}")
print("  => PASS" if (frozen and v_after == 'COERCIVE') else "  => CHECK output")
