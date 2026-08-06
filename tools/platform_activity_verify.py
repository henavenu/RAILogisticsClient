#!/usr/bin/env python3
"""Verify the platform Agent Activity lens: drive flagged work so the Sentinel loop
adjudicates, then confirm the governance-plane agents (gate, sentinel, the specialist
detectors) render in their OWN Agent Activity graph under app_id=trust-platform.

Requires: PE + Sentinel started with PLATFORM_ACTIVITY=true (and the relevant detectors
enabled). PAP up on :3002.
"""
import json, os, time, urllib.request

PE  = os.environ.get("PE_URL",  "http://127.0.0.1:3003")
PAP = os.environ.get("PAP_URL", "http://127.0.0.1:3002")
APP = "mybank"
PLAT = "trust-platform"
SID = f"platact-{int(time.time())}"


def post(base, path, body):
    r = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read().decode())


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=15) as resp:
        return json.loads(resp.read().decode())


def pay(amount, recipient, n):
    return post(PE, "/v1/app/evaluate", {
        "app_id": APP, "request_id": f"{SID}-p{n}", "role": "user_prompt",
        "content": f"Please wire ${amount:,} to {recipient} for invoice {n}.",
        "session_id": SID, "eval_mode": "SYNC",
        "metadata": {"amount": amount, "recipient_id": recipient},
    })


print(f"=== Platform Agent Activity verification (session={SID}) ===\n")
print("[1] Drive a structuring pattern so the gate flags it and the loop adjudicates")
for i, amt in enumerate([9800, 9950, 9700], 1):
    v = pay(amt, "OffshoreHoldings Ltd", i).get("verdict")
    print(f"    payment {i} (${amt:,}) -> {v}")

print("\n[2] Wait for the Sentinel loop to adjudicate + emit platform-activity (~10s)...")
time.sleep(12)

print("[3] Query the platform Agent Activity snapshot (app_id=trust-platform)")
snap = get(PAP, f"/v1/agent-topology/snapshot?app_id={PLAT}&window_min=60")
agents = snap.get("agents") or snap.get("nodes") or []
edges  = snap.get("edges") or []
names  = sorted({(a.get("id") or a.get("name") or a.get("agent")) for a in agents})
# edges are [source, target] pairs
edge_pairs = sorted({f"{e[0]}->{e[1]}" for e in edges if isinstance(e, (list, tuple)) and len(e) >= 2})
print(f"    nodes ({len(names)}): {names}")
print(f"    edges ({len(edge_pairs)}): {edge_pairs}")

have_sentinel = any("sentinel" in (n or "") for n in names)
have_detector = any("detector" in (n or "") for n in names)
have_edge     = any("->" in e and ("sentinel" in e or "gate" in e) for e in edge_pairs)

print("\n=== RESULT ===")
print(f"  platform graph has nodes            : {'PASS' if names else 'FAIL'}")
print(f"  sentinel node present               : {'PASS' if have_sentinel else 'FAIL'}")
print(f"  >=1 specialist detector node present: {'PASS' if have_detector else 'FAIL'}")
print(f"  governance-plane edge present       : {'PASS' if have_edge else 'FAIL'}")
ok = names and have_sentinel and have_detector
print("  => PASS" if ok else "  => CHECK output (is PLATFORM_ACTIVITY=true on PE+Sentinel?)")
