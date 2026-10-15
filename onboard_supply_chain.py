"""
VitaLink — Supply Chain Security pack (config only, via the Trust APIs). Idempotent.

Adds declarative Policy Builder rules for DEA suspicious-order monitoring, DSCSA
verification and cold-chain integrity, groups them in the `supply_chain_security`
pack, assigns the pack to `pharmalogistics` and publishes.

Agents report facts, the platform decides: each rule matches a fact the agent puts on
its tool call (e.g. 'cumulative_units_24h': 9000). Until an agent sends that fact the
rule scores 1 (not applicable), so adding the pack changes no existing verdict.

    venv/bin/python onboard_supply_chain.py
"""
from __future__ import annotations

import sys

import httpx
from dotenv import dotenv_values

from onboard_trust import APP_ID, HERE, _retry_insert

ACTOR = {"X-Actor": "pharmalogistics-onboarding"}
PACK_ID = "supply_chain_security"

#  dimension, display name, regex over the governed tool call, score, why, hitl_trigger
#  score 8 -> COERCIVE (BLOCK) · score 5 -> NORMATIVE (+ Human Review when hitl_trigger)
RULES = [
    # DEA controlled substances (21 CFR 1301.74 suspicious-order monitoring)
    ("dea_som_threshold", "Controlled-substance volume over SOM threshold",
     r"'cumulative_units_24h': (85(0[1-9]|[1-9]\d)|8[6-9]\d\d|9\d{3}|[1-9]\d{4,})(?!\d)", 8,
     "Cumulative Schedule II orders exceed the 8,500-unit DEA suspicious-order threshold (21 CFR 1301.74)", False),
    ("dea_registrant_unverified", "Buyer DEA registration not verified",
     r"'buyer_dea_verified': False", 8,
     "Controlled substances may only ship to a verified DEA registrant", False),
    ("dea_order_of_interest", "Order far above the buyer's normal volume",
     r"'order_vs_baseline_pct': ([3-9]\d\d|\d{4,})(?!\d)", 5,
     "Order is 300%+ of this buyer's baseline: hold for the SOM compliance officer", True),
    # DSCSA / EU GDP supply-chain integrity
    ("dscsa_product_unverified", "Product identifier not verified (DSCSA)",
     r"'serial_verified': False", 8,
     "Unit serial number not verified against the manufacturer (DSCSA)", False),
    ("unauthorized_trading_partner", "Trading partner not authorised",
     r"'trading_partner_authorized': False", 8,
     "Supplier or buyer is not an authorised trading partner (DSCSA / EU GDP)", False),
    ("cold_chain_unvalidated", "Cold-chain lane not validated",
     r"'cold_chain_validated': False", 8,
     "Temperature-sensitive product on a non-validated lane (EU GDP ch. 9)", False),
    ("temperature_excursion", "Temperature excursion recorded",
     r"'temp_excursion': True", 5,
     "Product left its storage band in transit: hold for Quality disposition", True),
]


def _rule_body(dim, name, pattern, score, why):
    return {"dimension_id": dim, "display_name": name, "category": "custom", "roles": ["tool_call", "agent_to_agent"],
            "rule": {"rule_id": f"{dim}_v1", "dimension": dim, "hit_policy": "F",
                     "columns": [{"field": "content.text", "type": "string"}],
                     "rows": [{"cells": {"content.text": {"op": "regex", "value": {"pattern": pattern, "case_sensitive": True}}},
                               "score": score, "reason": why}],
                     "otherwise": {"score": 1, "reason": "not applicable"}}}


def _policy_body(dim, why, score, hitl):
    coercive = score >= 7
    return {"dimension": dim, "app_id": APP_ID, "severity": 3 if coercive else 2,
            "thresholds": {"permissive": [1, 3], "normative": [4, 6] if coercive else [4, 9],
                           "coercive": [7, 9] if coercive else [10, 10]},
            "remediation_normative": why, "remediation_coercive": why if coercive else None,
            "policy_code": "VL-" + dim.upper()[:17], "normative_action": "HITL" if hitl else "REVISION",
            "normative_guidance": why, "human_note": why, "ops_alert": coercive,
            "hitl_trigger": hitl, "incident_routing": "platform_dashboard"}


PACK = {"id": PACK_ID, "definition": {
    "display_name": "Supply Chain Security",
    "description": "DEA suspicious-order monitoring, DSCSA verification, trading-partner authorisation "
                   "and cold-chain integrity",
    "layer": "domain-addon", "enforcement": "flexible", "mode": "collect-all", "combining_algorithm": "worst_case",
    "hitl_trigger": False, "owner": APP_ID, "version": "1.0.0", "active": True,
    "dimensions": [{"id": d, "profile": "standard", "severity": 3 if s >= 7 else 2, "abort": False,
                    "roles": ["tool_call", "agent_to_agent"], "note": n} for (d, n, _p, s, _w, _h) in RULES],
    "context_adjustments": [],
}}


def main() -> int:
    cfg = dotenv_values(HERE / "rai.env") if (HERE / "rai.env").exists() else {}
    pap_url = (cfg.get("RAI_PAP_URL") or "http://127.0.0.1:3002").split("#")[0].strip()
    pe_url = (cfg.get("RAI_PE_URL") or "http://127.0.0.1:3003").split("#")[0].strip()
    pap, pe = httpx.Client(base_url=pap_url, timeout=30), httpx.Client(base_url=pe_url, timeout=30)
    print(f"Trust platform: PAP={pap_url}  PE={pe_url}")
    failed = 0

    for (d, n, p, s, w, h) in RULES:
        r = pap.post("/v1/policy-builder/evaluation", json=_rule_body(d, n, p, s, w), headers=ACTOR)
        ok = r.status_code in (200, 201)
        failed += not ok
        verdict = "COERCIVE" if s >= 7 else "NORMATIVE" + (" + human review" if h else "")
        print(f"  {'✓' if ok else '✗'} rule {d} ({verdict})" + ("" if ok else f": HTTP {r.status_code} {r.text[:160]}"))

        body = _policy_body(d, w, s, h)
        r = _retry_insert(lambda: pap.post("/v1/policies", json=body, headers=ACTOR))
        if r.status_code == 409:
            r = pap.put(f"/v1/policies/{d}?app_id={APP_ID}",
                        json={k: v for k, v in body.items() if k not in ("dimension", "app_id")}, headers=ACTOR)
        if r.status_code >= 300:
            failed += 1
            print(f"  ✗ policy {d}: HTTP {r.status_code} {r.text[:160]}")

    packs = pe.get("/internal/packs").json()
    existing = {p.get("id") or p.get("pack_id") for p in (packs.get("packs", packs) if isinstance(packs, dict) else packs)}
    if PACK_ID in existing:
        r = pap.put(f"/v1/proxy/packs/{PACK_ID}", json=PACK["definition"], headers=ACTOR)
        print(f"  {'✓' if r.status_code < 300 else '✗'} {PACK_ID} pack updated: HTTP {r.status_code}")
    else:
        r = _retry_insert(lambda: pe.post("/internal/packs", json=PACK))
        print(f"  {'✓' if r.status_code < 300 else '✗'} {PACK_ID} pack created: HTTP {r.status_code}")
    failed += r.status_code >= 300

    r = _retry_insert(lambda: pe.post(f"/internal/app-stacks/{APP_ID}/packs/{PACK_ID}", json={"active": True}))
    print(f"  {'✓' if r.status_code < 300 else '✗'} {PACK_ID} assigned to {APP_ID}: HTTP {r.status_code}")
    failed += r.status_code >= 300

    r = pap.post("/v1/policies/publish", json={"notes": "supply_chain_security pack"}, headers=ACTOR)
    ok = r.status_code < 300 and r.json().get("success")
    print(f"  {'✓' if ok else '✗'} published (PE reload: {(r.json().get('reload') or {}).get('status') if ok else r.status_code})")
    failed += not ok
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
