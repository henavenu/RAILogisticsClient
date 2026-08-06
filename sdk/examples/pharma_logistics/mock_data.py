"""
NeuroTrust PharmaLogistics — In-memory mock supply-chain data.

Pharma distribution demo (Cencora-style). Contains the operating facility
profile, inventory positions, shipment/order history, and a ledger for orders
executed during the demo session.

Implementation note: internal dict keys ("checking"/"savings", field names like
"balance", "amount") are kept stable from the MyBank reference app so the
governance plumbing fires bit-identically. They are RE-THEMED at the presentation
layer: "checking" = the Central Rx Inventory account, "savings" = the Cold-Chain
Reserve, "balance" = on-hand inventory valuation (USD), "amount" = order value.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List

# ── Login credentials (demo only) ────────────────────────────────────────────
CREDENTIALS: dict = {
    "dana.reyes": {"pin": "1234", "customer_id": "cust_001"},
}

# ── Operating Facility / Operator Profile ─────────────────────────────────────
# (Kept under the key names the reference app expects; re-themed values.)

CUSTOMER: Dict[str, Any] = {
    "customer_id": "cust_001",
    "full_name": "Dana Reyes",                      # logistics operator on shift
    "email": "dana.reyes@riverside-dc.example",
    "phone": "+1 (614) 555-0192",
    "address": "Riverside Regional Distribution Center · 1400 Commerce Pkwy, Columbus, OH 43219",
    "date_of_birth": "—",
    "member_since": "2021-02-10",                    # facility onboarded
    "kyc_status": "LICENSE VERIFIED",               # DEA/state license standing
    "risk_tier": "LOW",
    # Pharma-specific identifiers (surfaced in profile view)
    "facility_name": "Riverside Regional DC",
    "dea_registration": "RR1234563",
    "vawd_accredited": True,
    "gdp_certified": True,
}

# ── Inventory Positions (accounts) ────────────────────────────────────────────
# Balances are on-hand inventory VALUATION in USD.

ACCOUNTS: Dict[str, Dict[str, Any]] = {
    "checking": {
        "account_id": "inv_central",
        "account_type": "Central Rx Inventory",
        "masked_number": "DC ····4521",
        "routing_number": "021000021",              # (facility routing / GLN stub)
        "balance": 486_200,                         # units on hand (dosage units / eaches)
        "currency": "units",
        "status": "ACTIVE",
        "available_balance": 486_200,
    },
    "savings": {
        "account_id": "inv_coldchain",
        "account_type": "Cold-Chain Reserve",
        "masked_number": "CC ····8834",
        "routing_number": "021000021",
        "balance": 73_400,                          # units on hand (refrigerated 2–8°C)
        "currency": "units",
        "status": "ACTIVE",
        "available_balance": 73_400,
        "apy": "2–8°C",                             # storage temp band
    },
}

# ── Inventory by Pharma Product Category (presentation layer) ─────────────────
# The two ACCOUNTS above are the governance-plumbing ledger (kept bit-identical
# to the reference app). An aggregate "units on hand" figure is meaningless to a
# DC operator, so the UI header renders the SAME on-hand position broken out by
# pharma product category — the way a distribution center actually tracks stock:
# handling class, storage temperature band, and regulatory schedule.
#
# Invariant: each account's categories sum to that account's seed balance.
#   checking (Central Rx Inventory) → 486,200 units
#   savings  (Cold-Chain Reserve)   →  73,400 units

_CATEGORY_SEED_BALANCE: Dict[str, int] = {"checking": 486_200, "savings": 73_400}

INVENTORY_CATEGORIES: List[Dict[str, Any]] = [
    {
        "category_id": "cat_controlled",
        "name": "Controlled Substances",
        "detail": "CII–CV · DEA vault",
        "units": 42_800,
        "account": "checking",
        "temp_band": "15–25°C",
        "regulated": True,
        "absorbs_drift": True,      # the only stock movement in this demo is CII
    },
    {
        "category_id": "cat_oral_solids",
        "name": "Oral Solids / Generics",
        "detail": "Ambient · bays 1–14",
        "units": 268_400,
        "account": "checking",
        "temp_band": "15–25°C",
        "regulated": False,
    },
    {
        "category_id": "cat_sterile_iv",
        "name": "Sterile IV & Infusion",
        "detail": "Ambient · bays 15–19",
        "units": 74_300,
        "account": "checking",
        "temp_band": "15–25°C",
        "regulated": False,
    },
    {
        "category_id": "cat_specialty",
        "name": "Specialty & Injectables",
        "detail": "Serialized · DSCSA",
        "units": 61_200,
        "account": "checking",
        "temp_band": "15–25°C",
        "regulated": True,
    },
    {
        "category_id": "cat_otc_supplies",
        "name": "OTC & Medical Supplies",
        "detail": "Ambient · bays 20–24",
        "units": 39_500,
        "account": "checking",
        "temp_band": "15–25°C",
        "regulated": False,
    },
    {
        "category_id": "cat_biologics",
        "name": "Cold-Chain Biologics",
        "detail": "Refrigerated · GDP",
        "units": 54_500,
        "account": "savings",
        "temp_band": "2–8°C",
        "regulated": True,
    },
    {
        "category_id": "cat_vaccines",
        "name": "Vaccines (Frozen)",
        "detail": "Freezer · lot-tracked",
        "units": 18_900,
        "account": "savings",
        "temp_band": "−20°C",
        "regulated": True,
    },
]


def inventory_categories() -> List[Dict[str, Any]]:
    """
    Category breakdown reconciled against the live account balances.

    Orders executed during the demo mutate ACCOUNTS[...]["balance"]. Each
    account nominates one category to absorb that drift so the displayed
    categories always re-sum to the live on-hand position.
    """
    drift: Dict[str, float] = {
        acct: ACCOUNTS[acct]["balance"] - seed
        for acct, seed in _CATEGORY_SEED_BALANCE.items()
    }
    out: List[Dict[str, Any]] = []
    for cat in INVENTORY_CATEGORIES:
        units = float(cat["units"])
        if cat.get("absorbs_drift"):
            units = max(0.0, units + drift.get(cat["account"], 0.0))
        out.append({**cat, "units": round(units), "currency": "units"})
    return out


# ── Inventory by Network Location (presentation layer) ────────────────────────
# The ACCOUNTS ledger is the PRIMARY distribution center only — that is the
# default fulfilment source, and the only site the demo's orders draw down.
# The remaining sites are network visibility: an operator checking stock wants to
# know what else the network holds before committing a transfer or a backorder.

INVENTORY_LOCATIONS: List[Dict[str, Any]] = [
    {
        "location_id": "DC-OH-01",
        "name": "Riverside Regional DC",
        "city": "Columbus, OH",
        "role": "Primary DC",
        "primary": True,
        "units": None,                        # derived from the live ledger
        "cold_chain": True,
        "license": "DEA RR1234563 · VAWD",
    },
    {
        "location_id": "DC-MO-04",
        "name": "Gateway Cross-Dock",
        "city": "St. Louis, MO",
        "role": "Cross-dock",
        "primary": False,
        "units": 212_400,
        "cold_chain": False,
        "license": "State WDD",
    },
    {
        "location_id": "DC-NC-02",
        "name": "Piedmont Specialty DC",
        "city": "Charlotte, NC",
        "role": "Specialty",
        "primary": False,
        "units": 168_900,
        "cold_chain": True,
        "license": "DEA RR9820114 · VAWD",
    },
    {
        "location_id": "DC-AZ-07",
        "name": "Sonoran Cold-Chain Hub",
        "city": "Phoenix, AZ",
        "role": "Cold-chain hub",
        "primary": False,
        "units": 96_300,
        "cold_chain": True,
        "license": "GDP certified",
    },
    {
        "location_id": "FD-NJ-11",
        "name": "Northeast Forward Depot",
        "city": "Edison, NJ",
        "role": "Forward depot",
        "primary": False,
        "units": 74_800,
        "cold_chain": False,
        "license": "State WDD",
    },
]


def inventory_locations() -> List[Dict[str, Any]]:
    """
    Network stock by site. The primary DC's figure is derived from the live
    ACCOUNTS ledger so it moves with orders executed during the session.
    """
    primary_units = sum(a["balance"] for a in ACCOUNTS.values())
    out: List[Dict[str, Any]] = []
    for loc in INVENTORY_LOCATIONS:
        units = primary_units if loc.get("primary") else float(loc["units"])
        out.append({**loc, "units": round(units), "currency": "units"})
    return out


# ── Shipment / Order Ledger (transactions) ────────────────────────────────────

TRANSACTIONS: Dict[str, List[Dict[str, Any]]] = {
    "checking": [
        {
            "txn_id": "txn_chk_001",
            "date": "2026-06-29",
            "type": "SHIP",
            "description": "OUTBOUND — Metro Community Pharmacy #4217 (amoxicillin, atorvastatin)",
            "amount": -8_743.00,
            "balance_after": 452_000.00,
            "category": "Outbound Shipment",
        },
        {
            "txn_id": "txn_chk_002",
            "date": "2026-06-29",
            "type": "RECV",
            "description": "INBOUND RECEIPT — Pfizer Wholesale (bulk generics)",
            "amount": 42_500.00,
            "balance_after": 460_743.00,
            "category": "Inbound Receipt",
        },
        {
            "txn_id": "txn_chk_003",
            "date": "2026-06-28",
            "type": "SHIP",
            "description": "OUTBOUND — Lakeside Health System (IV fluids, saline)",
            "amount": -13_499.00,
            "balance_after": 418_243.00,
            "category": "Outbound Shipment",
        },
        {
            "txn_id": "txn_chk_004",
            "date": "2026-06-28",
            "type": "ORDER",
            "description": "PO — Oakwood Clinics (unit-dose repackaging order)",
            "amount": -1_850.00,
            "balance_after": 431_742.00,
            "category": "Purchase Order",
        },
        {
            "txn_id": "txn_chk_005",
            "date": "2026-06-27",
            "type": "SHIP",
            "description": "OUTBOUND — Shell Point LTC Pharmacy (metformin, lisinopril)",
            "amount": -6_210.00,
            "balance_after": 433_592.00,
            "category": "Outbound Shipment",
        },
        {
            "txn_id": "txn_chk_006",
            "date": "2026-06-27",
            "type": "ADJ",
            "description": "CYCLE COUNT ADJUSTMENT — DC bay 12 reconciliation",
            "amount": -200.00,
            "balance_after": 439_802.00,
            "category": "Inventory Adjustment",
        },
        {
            "txn_id": "txn_chk_007",
            "date": "2026-06-26",
            "type": "RECV",
            "description": "INBOUND RECEIPT — Consignment restock (short-dated specials)",
            "amount": 15_000.00,
            "balance_after": 440_002.00,
            "category": "Inbound Receipt",
        },
        {
            "txn_id": "txn_chk_008",
            "date": "2026-06-26",
            "type": "SHIP",
            "description": "OUTBOUND — Riverbend Urgent Care (albuterol inhalers)",
            "amount": -1_199.00,
            "balance_after": 425_002.00,
            "category": "Outbound Shipment",
        },
        {
            "txn_id": "txn_chk_009",
            "date": "2026-06-25",
            "type": "SHIP",
            "description": "OUTBOUND — BlueRidge Specialty Pharmacy (biologics, refrigerated)",
            "amount": -3_120.00,
            "balance_after": 426_201.00,
            "category": "Cold-Chain Shipment",
        },
        {
            "txn_id": "txn_chk_010",
            "date": "2026-06-25",
            "type": "SHIP",
            "description": "OUTBOUND — Downtown Retail Pharmacy #1182 (OTC restock)",
            "amount": -785.00,
            "balance_after": 429_321.00,
            "category": "Outbound Shipment",
        },
    ],
    "savings": [
        {
            "txn_id": "txn_sav_001",
            "date": "2026-06-29",
            "type": "XFER",
            "description": "COLD-CHAIN TRANSFER FROM CENTRAL — vaccine lot VX-2291",
            "amount": 500.00,
            "balance_after": 452_000.00,
            "category": "Cold-Chain Transfer",
        },
        {
            "txn_id": "txn_sav_002",
            "date": "2026-06-29",
            "type": "MON",
            "description": "TEMP MONITORING PASS — reefer bank A (2–8°C, 0 excursions)",
            "amount": 159.43,
            "balance_after": 451_500.00,
            "category": "Cold-Chain Monitoring",
        },
        {
            "txn_id": "txn_sav_003",
            "date": "2026-06-15",
            "type": "XFER",
            "description": "COLD-CHAIN TRANSFER FROM CENTRAL — insulin restock",
            "amount": 500.00,
            "balance_after": 451_340.57,
            "category": "Cold-Chain Transfer",
        },
    ],
}

# ── Controlled-Substance Order Surge — Scenario 4 (Diversion Review Board) ──────
# A same-day burst of high-value controlled-substance orders from one buyer group
# grounds the diversion agent's "velocity anomaly" claim in visible evidence. A
# PENDING high-value order (SURGE_TRIGGER) lands on top and convenes the Diversion
# Review Board. This is deliberately distinct from the SOM $8,500 structuring rule
# and the cross-border / sanctions showcases.
CARD_LAST4 = "7731"   # buyer group PO series

CARD_TRANSACTIONS: List[Dict[str, Any]] = [
    {"txn_id": "ord_001", "time": "09:14", "merchant": "Valley Pharmacy Group — oxycodone 30mg",   "amount": 2450.00, "locale": "In-network",  "country": "US", "status": "approved"},
    {"txn_id": "ord_002", "time": "11:02", "merchant": "Valley Pharmacy Group — hydrocodone 10mg",  "amount": 3890.00, "locale": "In-network",  "country": "US", "status": "approved"},
    {"txn_id": "ord_003", "time": "13:37", "merchant": "Valley Pharmacy Group — oxycodone 15mg",    "amount": 1920.00, "locale": "In-network",  "country": "US", "status": "approved"},
    {"txn_id": "ord_004", "time": "15:48", "merchant": "Valley Pharmacy Group — alprazolam 2mg",    "amount": 4500.00, "locale": "New account", "country": "US", "status": "approved"},
]

# The pending high-value controlled-substance order that triggers the board review.
CARD_SURGE_TRIGGER: Dict[str, Any] = {
    "txn_id": "ord_pending", "time": "17:22", "merchant": "Valley Pharmacy Group — oxycodone 30mg (bulk)",
    "amount": 12000.00, "locale": "New account", "country": "US", "status": "pending_review",
}


def card_surge_summary() -> Dict[str, Any]:
    """Day-to-date controlled-substance order surge stats — used by the review board
    agents and Human Review evidence."""
    return {
        "card_last4":   CARD_LAST4,
        "today_count":  len(CARD_TRANSACTIONS),
        "today_total":  round(sum(t["amount"] for t in CARD_TRANSACTIONS), 2),
        "intl_count":   sum(1 for t in CARD_TRANSACTIONS if t["locale"] == "New account"),
        "pending":      CARD_SURGE_TRIGGER,
        "transactions": CARD_TRANSACTIONS,
    }


# ── Order Ledger ──────────────────────────────────────────────────────────────
# Populated at runtime as orders/shipments are executed during the demo session.

TRANSFER_LEDGER: List[Dict[str, Any]] = []

# ── Reset helper ──────────────────────────────────────────────────────────────
# Canonical initial valuations — used by the demo reset endpoint to restore
# inventory state without restarting the server process.

_INITIAL_BALANCES: Dict[str, Dict[str, float]] = {
    "checking": {"balance": 486_200, "available_balance": 486_200},
    "savings":  {"balance": 73_400, "available_balance": 73_400},
}

# Snapshot of the seed history, captured at import. reset_accounts() restores
# TRANSACTIONS to this so session orders (added by record_transaction) are cleared
# between demo runs — without restarting the server.
import copy as _copy
_INITIAL_TRANSACTIONS: Dict[str, List[Dict[str, Any]]] = _copy.deepcopy(TRANSACTIONS)

# Rolling cap: keep the most recent N ledger entries per account.
_TXN_CAP = 20


def record_transaction(
    account_key: str,
    txn_type: str,
    description: str,
    amount: float,
    category: str = "Order",
) -> None:
    """
    Prepend a ledger entry to an account's history so session activity (e.g. a
    domestic/cross-border shipment order) shows up in 'recent activity'. Newest-first,
    capped at _TXN_CAP (rotating).
    """
    from datetime import datetime as _dt
    import uuid as _uuid
    acct = ACCOUNTS.get(account_key, {})
    txn = {
        "txn_id":        f"txn_{account_key}_{_uuid.uuid4().hex[:8]}",
        "date":          _dt.now().strftime("%Y-%m-%d"),
        "type":          txn_type,
        "description":   description,
        "amount":        round(float(amount), 2),
        "balance_after": round(float(acct.get("balance", 0.0)), 2),
        "category":      category,
    }
    lst = TRANSACTIONS.setdefault(account_key, [])
    lst.insert(0, txn)               # newest first
    del lst[_TXN_CAP:]               # rotating cap


def reset_accounts() -> None:
    """Restore inventory valuations + ledger history to initial demo values and
    clear the order ledger (called by the demo /api/reset endpoint)."""
    for acct_key, initial in _INITIAL_BALANCES.items():
        if acct_key in ACCOUNTS:
            ACCOUNTS[acct_key].update(initial)
    TRANSACTIONS.clear()
    TRANSACTIONS.update(_copy.deepcopy(_INITIAL_TRANSACTIONS))
    TRANSFER_LEDGER.clear()


# ── Sourcing Options Catalog — Scenario 4 (Sourcing Recommendation Conflict) ────
# 5 supply sources for a therapy, with distinct sourcing profiles.
# Engineered conflict: lowest-cost ranking ≠ qualified-supplier suitability ranking.
#   GrayMarket Discount Intl: best raw price/availability (criteria top pick)
#   Cencora Qualified GPO:    best real-world fit / compliant (suitability top pick)
#
# NOTE: dict FIELD names are kept identical to the reference app's card catalog so
# the scoring tools work unchanged. They are re-themed in the tool docstrings and
# UI: country_coverage → network reach, forex_fee → landed-cost premium,
# rewards_rate → service level, annual_fee → contract fee, lounge_access →
# validated cold-chain, income_minimum → min order, credit_score_min → qualification.

CREDIT_CARDS: Dict[str, Dict[str, Any]] = {
    "global_elite_platinum": {
        "card_id":          "graymarket_discount_intl",
        "name":             "GrayMarket Discount Intl",
        "issuer":           "Unverified secondary wholesaler",
        "country_coverage": 140,     # network reach (broad)
        "forex_fee":        "0.0%",  # landed-cost premium (cheapest)
        "rewards_rate":     "Bulk discount up to 22%, next-day availability",
        "annual_fee":       "$595",  # contract / onboarding fee
        "income_minimum":   150_000,
        "credit_score_min": 750,     # qualification bar (this source barely clears docs)
        "lounge_access":    True,     # claims validated cold-chain
        "travel_insurance": True,
        "highlights":       "Lowest landed cost · broad availability · secondary-market source · pedigree gaps",
    },
    "travel_prime_visa": {
        "card_id":          "cencora_qualified_gpo",
        "name":             "Cencora Qualified GPO",
        "issuer":           "Cencora primary distribution",
        "country_coverage": 95,
        "forex_fee":        "0.5%",
        "rewards_rate":     "Contract pricing, DSCSA T3 pedigree, 2-hr support",
        "annual_fee":       "$295",
        "income_minimum":   80_000,
        "credit_score_min": 700,
        "lounge_access":    True,
        "travel_insurance": True,
        "highlights":       "Best qualified-supplier fit · full DSCSA pedigree · validated cold-chain · GPO contract",
    },
    "world_miles_mastercard": {
        "card_id":          "regional_authorized_dist",
        "name":             "Regional Authorized Distributor",
        "issuer":           "State-licensed wholesaler",
        "country_coverage": 65,
        "forex_fee":        "1.5%",
        "rewards_rate":     "Standard contract, T3 pedigree, next-day",
        "annual_fee":       "$150",
        "income_minimum":   60_000,
        "credit_score_min": 680,
        "lounge_access":    False,
        "travel_insurance": True,
        "highlights":       "Licensed + pedigree · moderate cost · strong regional coverage",
    },
    "everyday_rewards_gold": {
        "card_id":          "manufacturer_direct",
        "name":             "Manufacturer Direct",
        "issuer":           "OEM manufacturer",
        "country_coverage": 45,
        "forex_fee":        "2.5%",
        "rewards_rate":     "List pricing, longest lead time",
        "annual_fee":       "$95",
        "income_minimum":   40_000,
        "credit_score_min": 650,
        "lounge_access":    False,
        "travel_insurance": False,
        "highlights":       "Fully compliant · higher cost · limited network · long lead time",
    },
    "standard_card_blue": {
        "card_id":          "spot_buy_broker",
        "name":             "Spot-Buy Broker",
        "issuer":           "Unqualified broker",
        "country_coverage": 30,
        "forex_fee":        "3.0%",
        "rewards_rate":     "Spot pricing, no service commitment",
        "annual_fee":       "$0",
        "income_minimum":   25_000,
        "credit_score_min": 620,
        "lounge_access":    False,
        "travel_insurance": False,
        "highlights":       "No onboarding fee · basic availability · unqualified · entry-level source",
    },
}


# ── Facility Sourcing Profile — Scenario 4 ─────────────────────────────────────
# Supplements CUSTOMER for supplier suitability scoring.
# Conflict: GrayMarket has best raw price/availability, but Cencora Qualified GPO
# is the better real-world fit for this facility's compliance posture and volume.

CUSTOMER_FINANCIAL: Dict[str, Any] = {
    "annual_income":    245_000,     # annual procurement spend on this therapy class
    "net_worth":        1_500_000,   # total inventory valuation
    "credit_score":     785,         # compliance / audit standing
    "travel_frequency": "frequent",  # order frequency
    "trips_per_year":   14,          # orders/month peak
    "primary_regions":  ["Northeast", "Midwest", "Southeast"],
    "existing_cards":   ["standard_card_blue"],   # current spot-buy exposure to unwind
    "risk_profile":     "moderate",
    "account_tier":     "premium",   # GPO tier
}
