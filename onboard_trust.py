"""Onboard PharmaLogistics onto a Trust platform (local beta or GCP) — config only.

    venv/bin/python onboard_trust.py [--pap URL] [--pe URL] [--rotate-key]

Idempotent: each step skips what already exists. Steps:
  1. Register the app (POST /v1/apps) and write the one-time credential to .env (RAI_APP_KEY).
  2. Assign the policy packs (same set as the platform's MyBank reference app).
  3. Agent-passport enforcement — a declarative rule, an app-only pack and an app-scoped
     hitl_trigger policy: a governed action stamped `passport_oversight: HITL` is held
     for human review (NORMATIVE + REQUIRE_HUMAN_REVIEW); HOTL / HIC / QP-CERTIFIED pass.
  4. Publish, so the Policy Engine reloads its policies.
  5. Agent cards: the operator half of each agent's registry card (name, purpose, authority,
     data access, allowed / prohibited actions, escalation, kill switch, HITL mode) from
     agent_cards.json. Needs the agents registered first — start the app once, then run this.

The secret is never printed. URLs default to rai.env (RAI_PAP_URL / RAI_PE_URL).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent
APP_ID = "pharmalogistics"
ACTOR = {"X-Actor": "pharmalogistics-onboarding"}
PACKS = ["session_guard", "sanctions", "prompt_filters", "eu_ai_act", "agent_coordination",
         "sanctions_advanced", "agent_passport"]

APP = {
    "app_id": APP_ID, "display_name": "PharmaLogistics", "owner": "PharmaLogistics Global N.V.",
    "application_domain": "healthcare", "base_tier": "HIGH", "environment": "development",
    "description": "Multi-agent pharma distribution assistant: inventory, domestic replenishment, "
                   "US→EU cross-border release, diversion review.",
    "owner_team": "Global Quality & Supply Chain Systems",
    "intended_purpose": "Govern agent actions across order intake, cold-chain release and "
                        "controlled-substance oversight.",
    "regulatory_regimes": ["DSCSA", "DEA CSA", "FDA 21 CFR 211", "EU GDP 2013/C 343/01",
                           "EU FMD 2011/62/EU", "EU AI Act", "GDPR"],
    "sensitive_use": True,
}

PASSPORT_RULE = {
    "dimension_id": "passport_oversight", "display_name": "Agent Passport Oversight", "category": "custom",
    "roles": ["tool_call", "agent_to_agent"],
    "rule": {
        "rule_id": "passport_oversight_v1", "dimension": "passport_oversight", "hit_policy": "F",
        "columns": [{"field": "content.text", "type": "string"}],
        "rows": [{"cells": {"content.text": {"op": "regex", "value": {
                     "pattern": r"passport_oversight\W{1,6}HITL(?![A-Za-z0-9_])", "case_sensitive": True}}},
                  "score": 5, "reason": "Agent passport stamp requires human-in-the-loop for this jurisdiction"}],
        "otherwise": {"score": 1, "reason": "passport oversight is not HITL"},
    },
}

PASSPORT_PACK = {"id": "agent_passport", "definition": {
    "display_name": "Agent Passport",
    "description": "Holds actions whose agent-passport stamp requires human-in-the-loop oversight",
    "layer": "domain-addon", "enforcement": "flexible", "mode": "collect-all",
    "combining_algorithm": "worst_case", "hitl_trigger": False, "owner": APP_ID,
    "version": "1.0.0", "active": True,
    # profile 'standard' → score 5 lands in the NORMATIVE band. No context_adjustments:
    # a +2 step would push 5 into COERCIVE.
    "dimensions": [{"id": "passport_oversight", "profile": "standard", "severity": 2, "abort": False,
                    "roles": ["tool_call", "agent_to_agent"], "note": "score 5 = HITL stamp -> NORMATIVE"}],
    "context_adjustments": [],
}}

PASSPORT_POLICY = {
    "dimension": "passport_oversight", "app_id": APP_ID, "severity": 2,
    "thresholds": {"permissive": [1, 3], "normative": [4, 9], "coercive": [10, 10]},
    "remediation_normative": "This cross-border release is held: the agent passport stamp for this "
                             "jurisdiction requires human-in-the-loop approval (EU Qualified Person).",
    "policy_code": "PASSPORT-01", "normative_action": "HITL",
    "normative_guidance": "Route to the EU release desk for Qualified Person certification before release.",
    "human_note": "Verify the QP certification for the batch before approving.",
    "ops_alert": False, "hitl_trigger": True, "incident_routing": "platform_dashboard",
}


def _retry_insert(fn, tries: int = 25):
    """Beta's seeded Postgres sequences can lag the seeded rows, so a first insert may
    collide (500). Each failed insert advances the sequence; retry until it lands."""
    r = fn()
    for _ in range(tries):
        if r.status_code < 500:
            return r
        r = fn()
    return r


def _save_key(key: str) -> None:
    env = HERE / ".env"
    if not env.exists():
        env.write_text((HERE / ".env.example").read_text())
    text = env.read_text()
    line = f"RAI_APP_KEY={key}"
    text = re.sub(r"(?m)^RAI_APP_KEY=.*$", line, text) if re.search(r"(?m)^RAI_APP_KEY=", text) else text + f"\n{line}\n"
    env.write_text(text)
    env.chmod(0o600)


def main() -> int:
    cfg = dotenv_values(HERE / "rai.env") if (HERE / "rai.env").exists() else {}
    ap = argparse.ArgumentParser()
    ap.add_argument("--pap", default=(cfg.get("RAI_PAP_URL") or "http://127.0.0.1:3002").split("#")[0].strip())
    ap.add_argument("--pe", default=(cfg.get("RAI_PE_URL") or "http://127.0.0.1:3003").split("#")[0].strip())
    ap.add_argument("--rotate-key", action="store_true", help="issue a new app credential even if the app exists")
    a = ap.parse_args()
    pap, pe = httpx.Client(base_url=a.pap, timeout=30), httpx.Client(base_url=a.pe, timeout=30)
    print(f"Trust platform: PAP={a.pap}  PE={a.pe}")

    # 1. app + credential
    exists = pap.get(f"/v1/apps/{APP_ID}").status_code == 200
    cred = None
    if not exists:
        r = _retry_insert(lambda: pap.post("/v1/apps", json=APP, headers=ACTOR))
        if r.status_code == 201:
            cred = r.json().get("credential")
        elif r.status_code != 409:
            print(f"  ✗ register app: HTTP {r.status_code} {r.text[:200]}"); return 1
        print("  ✓ app registered")
        exists = True
    else:
        print("  · app already registered")
    if cred is None and (a.rotate_key or not (HERE / ".env").exists()
                         or not re.search(r"(?m)^RAI_APP_KEY=.+", (HERE / ".env").read_text())):
        r = _retry_insert(lambda: pap.post(f"/v1/apps/{APP_ID}/credentials", headers=ACTOR))
        if r.status_code != 201:
            print(f"  ✗ issue credential: HTTP {r.status_code} {r.text[:200]}"); return 1
        cred = r.json()
    if cred:
        _save_key(f"{cred['key_id']}.{cred['app_secret']}")
        print("  ✓ app credential written to .env (RAI_APP_KEY)")
    else:
        print("  · keeping existing RAI_APP_KEY in .env (use --rotate-key to replace)")

    # 3a/b. passport rule + pack (before assignment, so agent_passport exists)
    r = pap.post("/v1/policy-builder/evaluation", json=PASSPORT_RULE, headers=ACTOR)   # upsert
    ok = r.status_code in (200, 201)
    print(f"  {'✓' if ok else '✗'} passport_oversight rule ({r.json().get('action') if ok else f'HTTP {r.status_code}'})")
    packs = pe.get("/internal/packs").json()
    pack_ids = {p.get("id") or p.get("pack_id") for p in (packs.get("packs", packs) if isinstance(packs, dict) else packs)} \
        if isinstance(packs, (dict, list)) else set()
    if "agent_passport" not in pack_ids:
        r = _retry_insert(lambda: pe.post("/internal/packs", json=PASSPORT_PACK))
        print(f"  {'✓' if r.status_code < 300 else '✗'} agent_passport pack: HTTP {r.status_code}")
    else:
        print("  · agent_passport pack exists")

    # 2. packs
    for pk in PACKS:
        r = _retry_insert(lambda: pe.post(f"/internal/app-stacks/{APP_ID}/packs/{pk}", json={"active": True}))
        print(f"  {'✓' if r.status_code < 300 else '✗'} pack {pk}: HTTP {r.status_code}")

    # 3c. app-scoped hitl_trigger policy
    r = _retry_insert(lambda: pap.post("/v1/policies", json=PASSPORT_POLICY, headers=ACTOR))
    if r.status_code == 409:
        r = pap.put(f"/v1/policies/passport_oversight?app_id={APP_ID}",
                    json={k: v for k, v in PASSPORT_POLICY.items() if k not in ("dimension", "app_id")}, headers=ACTOR)
    print(f"  {'✓' if r.status_code < 300 else '✗'} passport_oversight policy (hitl_trigger): HTTP {r.status_code}")

    # 4. publish
    r = pap.post("/v1/policies/publish", json={"notes": "pharmalogistics onboarding"}, headers=ACTOR)
    ok = r.status_code < 300 and r.json().get("success")
    print(f"  {'✓' if ok else '✗'} published (PE reload: {(r.json().get('reload') or {}).get('status') if ok else r.status_code})")

    # 5. agent cards (operator metadata over the SDK-discovered cards)
    cards = json.loads((HERE / "agent_cards.json").read_text())
    done = 0
    for agent_id, c in cards.items():
        meta = {k: v for k, v in c.items() if k not in ("kill_switch", "hitl_mode")}
        try:
            rm = _retry_insert(lambda: pap.patch(f"/v1/agent-cards/{APP_ID}/{agent_id}/metadata", json=meta, headers=ACTOR), tries=3)
            rf = pap.patch(f"/v1/agent-cards/{APP_ID}/{agent_id}", headers=ACTOR,
                           json={"kill_switch": c.get("kill_switch"), "hitl_mode": c.get("hitl_mode")})
        except httpx.HTTPError as exc:
            print(f"  ✗ card {agent_id}: {type(exc).__name__} — PAP rejected the update (see pap.log)")
            continue
        if rm.status_code < 300 and rf.status_code < 300:
            done += 1
        else:
            print(f"  ✗ card {agent_id}: metadata HTTP {rm.status_code}, flags HTTP {rf.status_code} {rm.text[:120]}")
    print(f"  {'✓' if done == len(cards) else '✗'} agent cards enriched: {done}/{len(cards)}"
          + ("" if done else " (start the app first so the SDK registers its agents)"))
    return 0 if ok and done == len(cards) else 1


if __name__ == "__main__":
    sys.exit(main())
