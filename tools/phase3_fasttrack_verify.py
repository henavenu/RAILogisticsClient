#!/usr/bin/env python3
"""Phase 3 verification: SYNC fast-track adjudication for the WATCH case.

A deliberately over-sensitive gate flags structuring at just 2 band-hugging payments
(WATCH, not yet CONFIRMED). With PE_SYNC_FASTTRACK=true the PE makes ONE time-boxed
agentic call in-line: the agent UPHOLDS the suspicion and the SAME response carries a
REQUIRE_HUMAN_REVIEW directive — no waiting for the async loop. If the agent cannot
answer within the budget, the tier-strict fallback fails closed (structuring=strict).

Requires: PE started with PE_SYNC_FASTTRACK=true (+ generous PE_FASTTRACK_BUDGET_MS for
          a cold first call); Sentinel up with /internal/detect-sync.
"""
import json, os, time, urllib.request

PE  = os.environ.get("PE_URL", "http://127.0.0.1:3003")
APP = "mybank"
SID = f"phase3-fasttrack-{int(time.time())}"


def post(path, body):
    r = urllib.request.Request(PE + path, data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read().decode())


def get(path):
    with urllib.request.urlopen(PE + path, timeout=15) as resp:
        return json.loads(resp.read().decode())


def pay(amount, recipient, n):
    return post("/v1/app/evaluate", {
        "app_id": APP, "request_id": f"{SID}-p{n}", "role": "user_prompt",
        "content": f"Please wire ${amount:,} to {recipient} for invoice {n}.",
        "session_id": SID, "eval_mode": "SYNC",
        "metadata": {"amount": amount, "recipient_id": recipient},
    })


print(f"=== Phase 3 sync fast-track verification (session={SID}) ===\n")
print("[1] Two band-hugging payments to the same recipient (deterministic WATCH @ 2)")
r1 = pay(9800, "OffshoreHoldings Ltd", 1)
print(f"    payment 1 -> verdict={r1.get('verdict')}  directives={len(r1.get('directives') or [])}")
t0 = time.time()
r2 = pay(9700, "OffshoreHoldings Ltd", 2)
dt = (time.time() - t0) * 1000.0
print(f"    payment 2 -> verdict={r2.get('verdict')}  directives={len(r2.get('directives') or [])}  (round-trip {dt:.0f}ms)")

risk = get(f"/internal/session-risk/{SID}?app_id={APP}")
print(f"    risk_flags={risk.get('risk_flags')}  rollup={risk.get('risk_verdict')}")

# Inspect the 2nd response for the in-line fast-track HITL directive.
dirs = r2.get("directives") or []
ft = next((d for d in dirs
           if str(d.get("action", "")).lower() == "require_human_review"
           and (d.get("metadata") or {}).get("fasttrack")), None)
hitl_inline = ft is not None
upheld = bool(ft and not (ft.get("metadata") or {}).get("fallback"))
fallback = bool(ft and (ft.get("metadata") or {}).get("fallback"))
if ft:
    md = ft.get("metadata") or {}
    print(f"    >> fast-track directive: category={ft.get('category')} "
          f"fallback={md.get('fallback')} reason={str(ft.get('reason',''))[:90]}")

print("\n=== RESULT ===")
print(f"  WATCH raised at 2 payments          : {'PASS' if str(risk.get('risk_flags',{}).get('structuring','')).upper() in ('WATCH','CONFIRMED') else 'FAIL'}")
print(f"  in-line HITL on the SAME response   : {'PASS' if hitl_inline else 'FAIL'}")
print(f"  path                                : {'UPHELD by agent' if upheld else ('TIER-STRICT FAIL-CLOSED' if fallback else 'n/a')}")
print("  => PASS" if hitl_inline else "  => CHECK output")
