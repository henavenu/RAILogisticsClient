#!/usr/bin/env python3
"""Local smoke test: quick actions + showcase against the running :5000 stack."""
import json, os, sys, urllib.request
from collections import Counter

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:5000")

def post(path, body, timeout=180):
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def quick_actions():
    print("=== LOCAL #4a: QUICK ACTIONS (:5000) ===")
    sid = "local-smoke-py"
    msgs = ["check my balance", "show my recent transactions", "show my profile",
            "send 400 to Bob via ACH", "wire 5000 to Germany IBAN DE89370400440532013000"]
    ok = 0
    for m in msgs:
        try:
            d = post("/api/chat", {"message": m, "session_id": sid})
            tag = "BLOCK" if d.get("blocked") else "OK"
            if tag == "OK" or d.get("hitl_pending"):
                ok += 1
            print(f"  [{tag:5}] {m[:44]:44} verdict={d.get('verdict')} "
                  f"pii={d.get('pii_detected')} hitl={d.get('hitl_pending')} "
                  f"reply={len(d.get('reply') or '')}c")
        except Exception as e:
            print(f"  [ERR  ] {m[:44]:44} {e}")
    print(f"  => {ok}/{len(msgs)} handled")

def showcase():
    print("=== LOCAL #4b: SHOWCASE (:5000) ===")
    scen = ["structuring", "sanctions", "dispute", "scope_excursion",
            "pii_exfiltration", "advisory", "collusion"]
    def verdict(x):
        for k in ("verdict", "outcome", "decision", "status"):
            if x.get(k):
                return str(x[k])
        return "?"
    for s in scen:
        try:
            d = post("/api/showcase/" + s, None)
            r = d.get("results", [])
            vs = Counter(verdict(x) for x in r)
            print(f"  {s:16} {dict(vs)}")
        except Exception as e:
            print(f"  {s:16} ERR {e}")

if __name__ == "__main__":
    if "showcase" in sys.argv:
        showcase()
    elif "quick" in sys.argv:
        quick_actions()
    else:
        quick_actions(); showcase()
