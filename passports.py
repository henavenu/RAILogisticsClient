"""
Agent passports for the PharmaLogistics fleet.

Every agent carries a passport: who owns it, what it may do and where, how it is
overseen, and when the grant expires. The app owns the passports (the Trust platform
has no passport model yet); the platform enforces what a passport declares through
ordinary policy — see `stamp_action()` and README "Passport enforcement".

Risk tier follows the RAI primer ("Assigning the risk tier"):
  • Three dimensions — agency, impact, reach — each scored Tier 1..4;
    the highest one reached sets the tier.
  • Two amplifiers — coordination complexity, trust-boundary exposure — answered
    yes/no; each yes raises the tier one level. Swarm behaviour or a
    cross-jurisdiction boundary sets Tier 4 alone.
  • Human oversight follows agency: Instruct / Assist → HITL · Execute → HOTL ·
    Delegate → HIC. Oversight is stamped PER JURISDICTION, because what a country
    authorises can be narrower than what the agent is built to do.

One fleet, two postures: the Cross-Border Logistics agent acts in the US and the
EU under ONE identity. The US authorises Execute (human on the loop); the EU
authorises only Assist — a Qualified Person confirms every release. Its capability
ceiling (Execute) exceeds what the EU authorises; that gap is closed by policy,
not by the model, and the passport is where an auditor sees it.
"""
from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional

# ── The primer's scales ───────────────────────────────────────────────────────

TIER_NAMES = {1: "Limited", 2: "Elevated", 3: "High", 4: "Critical"}

AGENCY = {          # level → (tier, label)
    "instruct": (1, "Instruct · no discretion"),
    "assist":   (2, "Assist · human confirms"),
    "execute":  (3, "Execute · human on call"),
    "delegate": (4, "Delegate · operates independently"),
}
OVERSIGHT_FOR_AGENCY = {"instruct": "HITL", "assist": "HITL", "execute": "HOTL", "delegate": "HIC"}
OVERSIGHT_LABEL = {
    "HITL": "Human in the loop — every consequential action is approved by a human",
    "HOTL": "Human on the loop — the agent acts; a human monitors and can intervene",
    "HIC":  "Human in command — oversight at the system level, per mandate",
}
IMPACT = {
    1: "Low impact · fully reversible",
    2: "Limited consequences · easily corrected",
    3: "Significant impact · difficult to reverse",
    4: "Severe impact · irreversible harm to rights, safety or finances",
}
REACH = {
    1: "Single isolated system or dataset",
    2: "Multiple internal systems · within one business unit",
    3: "External systems or regulated data",
    4: "Open-ended ecosystem · cross-organizational or public reach",
}
TRUST_BOUNDARY = {   # value → (amplifier yes?, sets Tier 4 alone?, label)
    "single":              (False, False, "Single trust boundary · one organization, one system"),
    "internal":            (False, False, "Inside the organization's own systems"),
    "cross-platform":      (True,  False, "Cross-platform"),
    "cross-jurisdiction":  (True,  True,  "Cross-jurisdiction"),
}
COORDINATION = {     # value → (amplifier yes?, sets Tier 4 alone?, label)
    "single":      (False, False, "Single agent · no delegation to or from other agents"),
    "sequential":  (True,  False, "Sequential workflow"),
    "supervised":  (True,  False, "Supervised orchestration"),
    "swarm":       (True,  True,  "Swarm behaviour · no central controller"),
}
# Higher tier buys a shorter leash (AGENT_PASSPORT_DESIGN.md §"Validity derived from tier").
VALIDITY_MONTHS = {4: 6, 3: 12, 2: 24, 1: 36}

ISSUED = date(2026, 10, 13)


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    return d.replace(year=d.year + y, month=m + 1)


def assign_tier(agency: str, impact: int, reach: int, coordination: str, trust_boundary: str) -> dict:
    """Primer rule: highest dimension sets the tier; each amplifier 'yes' adds one;
    swarm / cross-jurisdiction set Tier 4 alone. Returns the tier and its basis."""
    agency_tier = AGENCY[agency][0]
    dims = {"agency": agency_tier, "impact": impact, "reach": reach}
    base = max(dims.values())
    driver = max(dims, key=dims.get)

    c_yes, c_alone, _ = COORDINATION[coordination]
    b_yes, b_alone, _ = TRUST_BOUNDARY[trust_boundary]
    raised = []
    if c_yes:
        raised.append("coordination")
    if b_yes:
        raised.append("trust boundary")

    if c_alone or b_alone:
        tier = 4
        why = f"{'swarm behaviour' if c_alone else 'cross-jurisdiction exposure'} sets Tier 4 alone"
    else:
        tier = min(4, base + len(raised))
        why = f"{driver} sets Tier {base}" + (f"; +{len(raised)} for {', '.join(raised)}" if raised else "")
    return {"tier": tier, "name": TIER_NAMES[tier], "dimensions": dims, "amplifiers": raised, "basis": why}


# ── Fleet-wide facts ──────────────────────────────────────────────────────────

OWNER = "PharmaLogistics Global N.V."
DEPLOY = "Global Quality & Supply Chain Systems"
US_ENTITY = "PharmaLogistics Inc. — Trenton, NJ"
EU_ENTITY = "PharmaLogistics Europe B.V. — Amsterdam"
REAUTH = ["new tool", "new authority", "new jurisdiction", "new model", "tier change"]

US_LAW = "FDA 21 CFR 211 · DSCSA (DQSA Title II) · DEA CSA 21 CFR 1301"
EU_LAW = ("EU GDP Guidelines 2013/C 343/01 · Directive 2001/83/EC Art. 51 (QP certification) · "
          "Falsified Medicines Directive 2011/62/EU · EU AI Act Art. 14 · GDPR Ch. V")

EU_MEMBERS = {
    "AT": "Austria", "BE": "Belgium", "BG": "Bulgaria", "HR": "Croatia", "CY": "Cyprus",
    "CZ": "Czechia", "DK": "Denmark", "EE": "Estonia", "FI": "Finland", "FR": "France",
    "DE": "Germany", "GR": "Greece", "HU": "Hungary", "IE": "Ireland", "IT": "Italy",
    "LV": "Latvia", "LT": "Lithuania", "LU": "Luxembourg", "MT": "Malta", "NL": "Netherlands",
    "PL": "Poland", "PT": "Portugal", "RO": "Romania", "SK": "Slovakia", "SI": "Slovenia",
    "ES": "Spain", "SE": "Sweden",
}

_FLEET_MODEL = {
    "base_model": "Groq-hosted open-weight model (see LLM_SELECTION at startup)",
    "build": "stock instruct weights — no fine-tune; behaviour is prompt- and policy-defined",
    "weights_host": "Groq · US (us-east)",
    "runtime_params": "temperature 0 · deterministic",
}


def _us_stamp(agency: str, action: str, escalation: str) -> dict:
    return {
        "code": "US", "name": "United States", "legal_entity": US_ENTITY,
        "applicable_law": US_LAW, "authorized_action": action,
        "agency": agency, "agency_label": AGENCY[agency][1],
        "oversight": OVERSIGHT_FOR_AGENCY[agency], "escalation": escalation, "status": "active",
    }


def _passport(agent_id: str, name: str, role: str, *, agency: str, impact: int, reach: int,
              coordination: str = "single", trust_boundary: str = "internal",
              jurisdictions: Optional[List[dict]] = None, scope: str, regime: str,
              kill_cost: str, version: str, capability_ceiling: Optional[str] = None,
              sovereignty: Optional[dict] = None, extra_lineage: Optional[List[dict]] = None) -> dict:
    tier = assign_tier(agency, impact, reach, coordination, trust_boundary)
    stamps = jurisdictions or [_us_stamp(agency, scope, "Supervisor review on any policy hold")]
    return {
        "agent_id": agent_id,
        "name": name,
        "role": role,
        "passport_no": f"PL-{agent_id.upper().replace('_', '-')}-{version}",
        "version": version,
        "issued": ISSUED.isoformat(),
        "valid_until": _add_months(ISSUED, VALIDITY_MONTHS[tier["tier"]]).isoformat(),
        "risk_tier": tier,
        "agency": agency,
        "agency_label": AGENCY[agency][1],
        "capability_ceiling": capability_ceiling or AGENCY[agency][1],
        "impact": impact, "impact_label": IMPACT[impact],
        "reach": reach, "reach_label": REACH[reach],
        "coordination": coordination, "coordination_label": COORDINATION[coordination][2],
        "trust_boundary": trust_boundary, "trust_boundary_label": TRUST_BOUNDARY[trust_boundary][2],
        "jurisdictions": stamps,
        "cross_jurisdiction": len({s["code"] for s in stamps}) > 1,
        # Primer: every agent has a governance record. It stays in IDENTITY mode (the agent
        # card in the Trust registry) inside a trust domain the org controls, and is presented
        # in PASSPORT mode when the agent acts across a boundary it does not control.
        "record_mode": "passport" if TRUST_BOUNDARY[trust_boundary][0] else "identity",
        "sovereignty": sovereignty or {
            "acts_in": [s["code"] for s in stamps],
            "data_residency": {"US": "US (us-east)"},
            "model_region": "US",
            "transfer_basis": {"US": "domestic deployment — no cross-border transfer"},
        },
        "ownership": {"owner_org": OWNER, "deployment_owner": DEPLOY,
                      "legal_entities": {s["code"]: s["legal_entity"] for s in stamps}},
        "lineage": [
            {"from": f"{OWNER} (board delegation)", "to": DEPLOY,
             "scope": "commission the supply-chain agent fleet", "recorded": "2026-01-09"},
            {"from": DEPLOY, "to": name, "scope": scope, "recorded": "2026-01-22"},
        ] + (extra_lineage or []),
        "assurance": {
            "pre_release_eval": "Scenario bench — the app's governance scenarios replayed against expected verdicts",
            "certifying_regime": regime,
            "red_team": "Diversion, structuring and cold-chain evasion probes per release",
        },
        "reauth_triggers": REAUTH,
        "revocation": f"Trust platform → Agent registry → evict ({kill_cost})",
        "model": _FLEET_MODEL,
    }


# ── The cross-jurisdiction passport ───────────────────────────────────────────

_CROSS_BORDER = _passport(
    "intl_wire_agent", "Cross-Border Logistics",
    "Cross-border pharmaceutical shipments — customs, import licence, cold-chain release",
    agency="execute", impact=4, reach=3, coordination="single", trust_boundary="cross-jurisdiction",
    version="clr-3.1",
    capability_ceiling="Execute — built to release consignments unsupervised; the EU authorises Assist only",
    scope="release cold-chain consignments across the US → EU corridor",
    regime="FDA / DSCSA export audit (US) · EU GDP inspection by the national competent authority (EU)",
    kill_cost="all stamps void; in-flight consignments fall back to manual QP release",
    jurisdictions=[
        _us_stamp("execute", "Release a cold-chain consignment for export",
                  "Temperature excursion > 2 °C·h → US QA release officer"),
        {
            "code": "EU", "name": "European Union", "legal_entity": EU_ENTITY,
            "applicable_law": EU_LAW,
            "authorized_action": "Recommend release — a Qualified Person certifies every batch",
            "agency": "assist", "agency_label": AGENCY["assist"][1],
            "oversight": OVERSIGHT_FOR_AGENCY["assist"],
            "escalation": "Every release → Qualified Person (QP) certification, EU release desk",
            "status": "active",
        },
    ],
    sovereignty={
        "acts_in": ["US", "EU"],
        "data_residency": {"US": "US (us-east)", "EU": "EU (Frankfurt)"},
        "model_region": "US",
        "transfer_basis": {
            "US": "domestic deployment — no cross-border transfer",
            "EU": "EU–US Data Privacy Framework + SCCs (2021/914) — consignee data pseudonymised "
                  "before the inference call leaves the EU",
        },
    },
    extra_lineage=[
        {"from": f"{US_ENTITY} — QA release officer", "to": "US stamp",
         "scope": "export release inside the validated cold-chain envelope (Execute · HOTL)",
         "recorded": "2026-02-17"},
        {"from": f"{EU_ENTITY} — Qualified Person", "to": "EU stamp",
         "scope": "recommendation only (Assist · HITL); the QP certifies every release",
         "recorded": "2026-03-05"},
    ],
)

# ── The domestic fleet ────────────────────────────────────────────────────────

_FLEET = [
    _passport("router", "Order Intake", "Classifies each operator request and routes it to a specialist",
              agency="assist", impact=1, reach=2, coordination="sequential", version="intake-2.0",
              scope="accept and classify inbound orders", regime="Internal control attestation",
              kill_cost="order intake stops; orders queue for manual triage"),
    _passport("transfer_router", "Shipment Classifier", "Classifies a request as domestic or cross-border and hands it on",
              agency="assist", impact=1, reach=2, coordination="sequential", version="classify-1.2",
              scope="classify shipment requests as domestic or cross-border", regime="Internal control attestation",
              kill_cost="shipment requests queue for manual classification"),
    _passport("account", "Inventory Agent", "On-hand positions and the shipment/order ledger (read-only)",
              agency="instruct", impact=1, reach=2, version="inv-2.0",
              scope="read inventory positions and the order ledger", regime="Internal control attestation",
              kill_cost="inventory lookups stop; cached positions go stale"),
    _passport("profile", "Facility Desk", "Facility and operator profile, DEA/state licence standing (read-only)",
              agency="instruct", impact=1, reach=3, version="facility-2.0",
              scope="resolve facility and DEA-registration attributes",
              regime="DEA registrant verification (annual)", kill_cost="facility lookups stop"),
    _passport("domestic_agent", "Route Optimizer", "Domestic replenishment orders and US ground shipments",
              agency="execute", impact=3, reach=3, version="route-2.3",
              scope="place domestic replenishment orders (US ground)",
              regime="DEA Suspicious Order Monitoring programme audit",
              kill_cost="domestic ordering stops; the replenishment desk reverts to manual"),
    _CROSS_BORDER,
    _passport("payment_council", "Diversion Review", "Convenes the Diversion Review Board on a held order",
              agency="assist", impact=2, reach=2, coordination="supervised", version="board-1.6",
              scope="open a diversion review on a held controlled-substance order",
              regime="Internal RAI review board sign-off", kill_cost="held orders route straight to human review"),
    _passport("policy_council", "Diversion Review Board", "Tallies the board's votes and escalates a deadlock",
              agency="assist", impact=2, reach=2, coordination="supervised", version="board-1.6",
              scope="tally board votes; escalate an unresolved split to a human",
              regime="Internal RAI review board sign-off", kill_cost="held orders route straight to human review"),
    _passport("coordinator", "Supply Chain Coordinator", "Runs the board and hands off to reviewers",
              agency="assist", impact=2, reach=2, coordination="supervised", version="coord-2.1",
              scope="route review requests and escalate deadlocked board decisions",
              regime="Internal RAI review board sign-off",
              kill_cost="the board loses its convenor; every held order escalates to human review"),
    _passport("transaction_monitoring", "Order Monitoring Agent", "Velocity/anomaly lens on controlled-substance orders",
              agency="assist", impact=2, reach=2, coordination="supervised", version="som-2.2",
              scope="run the Suspicious Order Monitoring lens", regime="DEA SOM programme audit",
              kill_cost="SOM detection stops; ordering continues without this lens"),
    _passport("revenue_agent", "Supply Continuity Reviewer", "Weighs the impact of blocking a legitimate buyer",
              agency="assist", impact=2, reach=2, coordination="supervised", version="cont-1.4",
              scope="weigh supply-continuity and shortage impact", regime="Internal control attestation",
              kill_cost="the continuity lens is absent from board votes"),
    _passport("cx_agent", "Patient Access Reviewer", "Weighs downstream patient-care impact of a held order",
              agency="assist", impact=2, reach=2, coordination="supervised", version="access-1.4",
              scope="weigh patient-access impact on a held order", regime="Internal control attestation",
              kill_cost="the patient-access lens is absent from board votes"),
    _passport("risk_policy_agent", "Regulatory Compliance Reviewer", "DEA / DSCSA regulatory exposure",
              agency="assist", impact=2, reach=3, coordination="supervised", version="reg-2.2",
              scope="assess DEA / DSCSA regulatory and diversion exposure",
              regime="DEA registrant verification · DSCSA traceability audit",
              kill_cost="the regulatory lens is absent from board votes"),
    _passport("fallback", "Help Desk", "Answers requests no specialist matches; takes no actions",
              agency="instruct", impact=1, reach=1, version="help-1.0",
              scope="answer unmatched requests without acting", regime="Internal control attestation",
              kill_cost="unmatched requests get no reply"),
]

PASSPORTS: Dict[str, dict] = {p["agent_id"]: p for p in _FLEET}


# ── Runtime: what does the passport authorise HERE? ───────────────────────────

def jurisdiction_of(country: str) -> Optional[str]:
    """Map a destination country (ISO-2 or name) to the passport jurisdiction code."""
    c = (country or "").strip()
    if not c:
        return None
    up = c.upper()
    if up in ("US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"):
        return "US"
    if up in ("EU", "EUROPEAN UNION") or up in EU_MEMBERS:
        return "EU"
    if c.title() in EU_MEMBERS.values():
        return "EU"
    return None


def jurisdiction_from_swift(code: str) -> Optional[str]:
    """Customs/broker codes in this app are SWIFT-shaped: chars 5-6 are the country."""
    code = (code or "").strip().upper()
    return jurisdiction_of(code[4:6]) if len(code) in (8, 11) else None


def stamp_for(agent_id: str, jurisdiction: Optional[str]) -> Optional[dict]:
    p = PASSPORTS.get(agent_id)
    if not p or not jurisdiction:
        return None
    return next((s for s in p["jurisdictions"] if s["code"] == jurisdiction), None)


def stamp_action(agent_id: str, country: str = "", swift: str = "") -> dict:
    """The passport fields stamped onto a governed action. The platform's
    passport_oversight policy reads `passport_oversight` and holds HITL actions for
    human review; HOTL / HIC actions proceed under monitoring."""
    jur = jurisdiction_of(country) or jurisdiction_from_swift(swift)
    p = PASSPORTS.get(agent_id)
    s = stamp_for(agent_id, jur)
    if p is None:
        return {}
    if s is None:
        # No stamp for this jurisdiction → the passport does not authorise it at all.
        return {"passport_no": p["passport_no"], "passport_jurisdiction": jur or "unknown",
                "passport_oversight": "HITL", "passport_basis": "no stamp for this jurisdiction"}
    return {
        "passport_no": p["passport_no"],
        "passport_tier": f"T{p['risk_tier']['tier']}",
        "passport_jurisdiction": s["code"],
        "passport_agency": s["agency"],
        "passport_oversight": s["oversight"],
    }


def summary() -> List[dict]:
    """Compact rows for the passport list."""
    return [{
        "agent_id": p["agent_id"], "name": p["name"], "role": p["role"],
        "passport_no": p["passport_no"], "tier": p["risk_tier"]["tier"],
        "tier_name": p["risk_tier"]["name"], "basis": p["risk_tier"]["basis"],
        "jurisdictions": [{"code": s["code"], "agency": s["agency"], "oversight": s["oversight"]}
                          for s in p["jurisdictions"]],
        "cross_jurisdiction": p["cross_jurisdiction"], "valid_until": p["valid_until"],
        "record_mode": p["record_mode"],
    } for p in _FLEET]
