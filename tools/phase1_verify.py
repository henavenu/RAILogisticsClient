#!/usr/bin/env python3
"""Phase 1 verification: PE structuring accumulator + circuit-breaker, against the PE directly."""
import json, os, sys, time, urllib.request

PE  = os.environ.get("PE_URL", "http://127.0.0.1:3003")
APP = "mybank"
SID = f"phase1-verify-{int(time.time())}"


def post(path, body):
    r = urllib.request.Request(PE + path, data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=30) as resp:
        return resp.status, json.loads(resp.read().decode())


def get(path):
    with urllib.request.urlopen(PE + path, timeout=15) as resp:
        return resp.status, json.loads(resp.read().decode())


def pay(amount, recipient, n):
    body = {
        "app_id": APP, "request_id": f"{SID}-p{n}", "role": "user_prompt",
        "content": f"Please wire ${amount:,} to {recipient} for invoice {n}.",
        "session_id": SID, "eval_mode": "SYNC",
        "metadata": {"amount": amount, "recipient_id": recipient},
    }
    st, out = post("/v1/app/evaluate", body)
    return out.get("verdict")


print(f"=== Phase 1 verification (session={SID}) ===\n")

print("[1] Post a structuring sequence (3x just-under, same recipient)")
for i, amt in enumerate([9800, 9950, 9700], 1):
    v = pay(amt, "GlobalRemit Ltd", i)
    st, risk = get(f"/internal/session-risk/{SID}?app_id={APP}")
    print(f"    payment {i} (${amt:,}) -> verdict={v} | risk_record={risk.get('risk_verdict')}: "
          f"{(risk.get('risk_reason') or '')[:70]}")

st, risk = get(f"/internal/session-risk/{SID}?app_id={APP}")
print(f"\n    => standing risk_verdict = {risk.get('risk_verdict')}  (expected CONFIRMED)  "
      f"payment_count={risk.get('payment_count')}")
struct_ok = risk.get("risk_verdict") == "CONFIRMED"

print("\n[2] Breaker: freeze the session, then post one more action")
st, fr = post("/internal/session-freeze",
              {"session_id": SID, "app_id": APP, "frozen": True, "reason": "phase1 test freeze"})
print(f"    freeze set: {fr}")
v_frozen = pay(500, "Normal Payee", 99)
st, risk2 = get(f"/internal/session-risk/{SID}?app_id={APP}")
print(f"    next action verdict = {v_frozen}  (expected COERCIVE)   frozen={risk2.get('frozen')}")
breaker_ok = (v_frozen == "COERCIVE")

print("\n[3] Clear the freeze")
st, fr2 = post("/internal/session-freeze", {"session_id": SID, "app_id": APP, "frozen": False})
print(f"    cleared: {fr2}")

print("\n=== RESULT ===")
print(f"  structuring detected (CONFIRMED) : {'PASS' if struct_ok else 'FAIL'}")
print(f"  circuit-breaker blocks next act  : {'PASS' if breaker_ok else 'FAIL'}")
print("  => PASS" if (struct_ok and breaker_ok) else "  => CHECK output")
