#!/usr/bin/env python3
"""
Drive the full 2-stage card-denial HITL flow over the API and count the
hitl_checkpoint audit rows for the session — to verify fix #1 (no dual-side writes).

Flow:
  1. /api/chat "my card was declined"        -> council runs, HITL #1 (stage 1) fires
  2. PAP /v1/hitl/{sid}/resolve request_verification  (stage-1 reviewer action)
  3. /api/hitl/provide-info (customer answer) -> HITL #2 (stage 2) raised
  4. PAP /v1/hitl/{sid}/resolve approved       (stage-2 reviewer action)
  5. GET /v1/audit/evaluations -> count hitl_checkpoint rows for this session
"""
import json, os, sys, time, urllib.request
from collections import Counter

MYBANK = os.environ.get("CD_MYBANK", "http://127.0.0.1:5000")
PAP    = os.environ.get("CD_PAP",    "http://127.0.0.1:3002")
SID    = os.environ.get("CD_SID",    "card-denial-cleanup-001")

def _req(method, url, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data,
                               headers={"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        raw = resp.read().decode()
        try:
            return resp.status, json.loads(raw)
        except Exception:
            return resp.status, raw

def wait_pap_pending(sid, tries=40):
    for _ in range(tries):
        try:
            st, body = _req("GET", f"{PAP}/v1/hitl/{sid}")
            if isinstance(body, dict) and body.get("status") == "pending":
                return body
        except Exception:
            pass
        time.sleep(1)
    return None

print(f"=== Driving 2-stage card-denial flow (session={SID}) ===\n")

# 1) Trigger the council -> HITL #1
print("[1] POST /api/chat 'my card was declined' (council runs; may take ~10-20s)...")
st, body = _req("POST", f"{MYBANK}/api/chat",
                {"message": "my card was declined", "session_id": SID}, timeout=120)
print(f"    -> {st}  hitl_pending={isinstance(body,dict) and body.get('hitl_pending')}  "
      f"reply={(body.get('reply') if isinstance(body,dict) else '')[:60]!r}")

item1 = wait_pap_pending(SID)
print(f"[1b] PAP HITL #1 present: {bool(item1)}  stage={item1.get('stage') if item1 else '?'}\n")

# 2) Stage-1 reviewer action: request_verification (via PAP, the realistic path)
print("[2] POST PAP /v1/hitl/{sid}/resolve  decision=request_verification ...")
st, body = _req("POST", f"{PAP}/v1/hitl/{SID}/resolve",
                {"decision": "request_verification", "reviewer": "verify-bot"})
print(f"    -> {st}  {body}\n")
time.sleep(2)

# 3) Customer answers -> HITL #2 (stage 2)
print("[3] POST /api/hitl/provide-info (customer confirms purchase) ...")
st, body = _req("POST", f"{MYBANK}/api/hitl/provide-info",
                {"session_id": SID, "answer": "yes it was me, in Singapore in-store"})
print(f"    -> {st}  {body}")
item2 = wait_pap_pending(SID)
print(f"[3b] PAP HITL #2 present: {bool(item2)}  stage={item2.get('stage') if item2 else '?'}\n")

# 4) Stage-2 reviewer action: approved
print("[4] POST PAP /v1/hitl/{sid}/resolve  decision=approved ...")
st, body = _req("POST", f"{PAP}/v1/hitl/{SID}/resolve",
                {"decision": "approved", "reviewer": "verify-bot"})
print(f"    -> {st}  {body}\n")

# 5) Count hitl_checkpoint audit rows for this session
print("[5] Waiting for async audit writes, then counting hitl_checkpoint rows...")
time.sleep(6)
st, body = _req("GET", f"{PAP}/v1/audit/evaluations?app_id=mybank&limit=300")
recs = body.get("records", []) if isinstance(body, dict) else []
mine = [r for r in recs if r.get("session_id") == SID]

by_src_type = Counter()
hitl_rows = []
for r in mine:
    md = r.get("metadata_json") or r.get("metadata") or {}
    if isinstance(md, str):
        try: md = json.loads(md)
        except Exception: md = {}
    et  = md.get("event_type", "")
    src = md.get("source", "mybank" if et in ("hitl_initiation", "hitl_decision") else "?")
    if "hitl" in et or r.get("role") == "hitl_checkpoint":
        by_src_type[(src, et)] += 1
        hitl_rows.append((r.get("timestamp", "")[:19], src, et, md.get("decision", "")))

print(f"\n=== RESULT: {len(mine)} total rows for session, {len(hitl_rows)} HITL checkpoint rows ===")
for (src, et), n in sorted(by_src_type.items()):
    print(f"   {src:10} {et:18} x{n}")
print("\n   timeline:")
for ts, src, et, dec in sorted(hitl_rows):
    print(f"     {ts}  {src:10} {et:18} {dec}")

resolved = sum(n for (s, e), n in by_src_type.items() if e in ("hitl_resolved", "hitl_decision"))
mybank_dupes = sum(n for (s, e), n in by_src_type.items() if e in ("hitl_initiation", "hitl_decision"))
print(f"\n   resolution/decision rows : {resolved}  (expected 2 — one per stage)")
print(f"   MyBank-side duplicate rows: {mybank_dupes}  (expected 0 after fix #1)")
print("   => PASS" if resolved == 2 and mybank_dupes == 0 else "   => CHECK output above")
