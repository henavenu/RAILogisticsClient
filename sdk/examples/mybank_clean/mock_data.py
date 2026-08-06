"""
MyBank — In-memory mock banking data.

Contains customer profile, accounts, transaction history, and a ledger
for recording transfers executed during the demo session.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List

# ── Login credentials (demo only) ────────────────────────────────────────────
CREDENTIALS: dict = {
    "alex.morgan": {"pin": "1234", "customer_id": "cust_001"},
}

# ── Customer Profile ──────────────────────────────────────────────────────────

CUSTOMER: Dict[str, Any] = {
    "customer_id": "cust_001",
    "full_name": "Alex Morgan",
    "email": "alex.morgan@example.com",
    "phone": "+1 (415) 555-0192",
    "address": "742 Evergreen Terrace, Springfield, IL 62701",
    "date_of_birth": "1987-06-14",
    "member_since": "2019-03-22",
    "kyc_status": "VERIFIED",
    "risk_tier": "LOW",
}

# ── Accounts ──────────────────────────────────────────────────────────────────

ACCOUNTS: Dict[str, Dict[str, Any]] = {
    "checking": {
        "account_id": "acc_chk",
        "account_type": "Checking",
        "masked_number": "****4521",
        "routing_number": "021000021",
        "balance": 45_200.00,
        "currency": "USD",
        "status": "ACTIVE",
        "available_balance": 45_200.00,
    },
    "savings": {
        "account_id": "acc_sav",
        "account_type": "Savings",
        "masked_number": "****8834",
        "routing_number": "021000021",
        "balance": 1_247_500.00,
        "currency": "USD",
        "status": "ACTIVE",
        "available_balance": 1_247_500.00,
        "apy": "4.25%",
    },
}

# ── Transactions ──────────────────────────────────────────────────────────────

TRANSACTIONS: Dict[str, List[Dict[str, Any]]] = {
    "checking": [
        {
            "txn_id": "txn_chk_001",
            "date": "2026-04-09",
            "type": "POS",
            "description": "Whole Foods Market #4217",
            "amount": -87.43,
            "balance_after": 1_247_500.00,
            "category": "Groceries",
        },
        {
            "txn_id": "txn_chk_002",
            "date": "2026-04-08",
            "type": "ACH",
            "description": "PAYROLL DIRECT DEPOSIT - TechCorp Inc",
            "amount": 4_250.00,
            "balance_after": 1_247_587.43,
            "category": "Income",
        },
        {
            "txn_id": "txn_chk_003",
            "date": "2026-04-07",
            "type": "POS",
            "description": "Amazon.com Purchase",
            "amount": -134.99,
            "balance_after": 1_243_337.43,
            "category": "Shopping",
        },
        {
            "txn_id": "txn_chk_004",
            "date": "2026-04-06",
            "type": "ACH",
            "description": "RENT PAYMENT - Oakwood Properties LLC",
            "amount": -1_850.00,
            "balance_after": 1_243_472.42,
            "category": "Housing",
        },
        {
            "txn_id": "txn_chk_005",
            "date": "2026-04-05",
            "type": "POS",
            "description": "Shell Gas Station #0871",
            "amount": -62.10,
            "balance_after": 1_245_322.42,
            "category": "Auto & Transport",
        },
        {
            "txn_id": "txn_chk_006",
            "date": "2026-04-04",
            "type": "ATM",
            "description": "ATM WITHDRAWAL - Chase ATM #4482",
            "amount": -200.00,
            "balance_after": 1_245_384.52,
            "category": "Cash",
        },
        {
            "txn_id": "txn_chk_007",
            "date": "2026-04-03",
            "type": "WIRE",
            "description": "WIRE TRANSFER IN - Freelance Payment - DesignCo",
            "amount": 1_500.00,
            "balance_after": 1_245_584.52,
            "category": "Income",
        },
        {
            "txn_id": "txn_chk_008",
            "date": "2026-04-02",
            "type": "POS",
            "description": "Spotify Premium Subscription",
            "amount": -11.99,
            "balance_after": 1_244_084.52,
            "category": "Entertainment",
        },
        {
            "txn_id": "txn_chk_009",
            "date": "2026-04-01",
            "type": "ACH",
            "description": "INSURANCE PAYMENT - BlueCross BlueShield",
            "amount": -312.00,
            "balance_after": 1_244_096.51,
            "category": "Health",
        },
        {
            "txn_id": "txn_chk_010",
            "date": "2026-03-31",
            "type": "POS",
            "description": "Starbucks Coffee #1182",
            "amount": -7.85,
            "balance_after": 1_244_408.51,
            "category": "Food & Drink",
        },
    ],
    "savings": [
        {
            "txn_id": "txn_sav_001",
            "date": "2026-04-01",
            "type": "ACH",
            "description": "AUTO-TRANSFER FROM CHECKING",
            "amount": 500.00,
            "balance_after": 45_200.00,
            "category": "Transfer",
        },
        {
            "txn_id": "txn_sav_002",
            "date": "2026-04-01",
            "type": "ACH",
            "description": "INTEREST CREDIT - APY 4.25%",
            "amount": 159.43,
            "balance_after": 44_700.00,
            "category": "Interest",
        },
        {
            "txn_id": "txn_sav_003",
            "date": "2026-03-15",
            "type": "ACH",
            "description": "AUTO-TRANSFER FROM CHECKING",
            "amount": 500.00,
            "balance_after": 44_540.57,
            "category": "Transfer",
        },
    ],
}

# ── Card Activity — Story 5 (Holiday Card Surge → multi-agent authorization review) ──
# A same-day burst of high-value card purchases (several international, all NON-sanctioned
# countries) that grounds the fraud agent's "velocity anomaly" claim in visible evidence.
# A PENDING high-value international auth (CARD_SURGE_TRIGGER) lands on top of this surge
# and convenes the Payment Council. This is a CARD rail — deliberately distinct from the
# ACH $8,500 structuring rule and the international-wire / sanctions showcases.
CARD_LAST4 = "7731"   # Alex Morgan's primary Visa

CARD_TRANSACTIONS: List[Dict[str, Any]] = [
    {"txn_id": "card_001", "time": "09:14", "merchant": "Best Buy Electronics",    "amount": 2450.00, "locale": "Domestic",      "country": "US",             "status": "approved"},
    {"txn_id": "card_002", "time": "11:02", "merchant": "Mayfair Luxury Watches",  "amount": 3890.00, "locale": "International",  "country": "United Kingdom", "status": "approved"},
    {"txn_id": "card_003", "time": "13:37", "merchant": "Delta Air Lines",         "amount": 1920.00, "locale": "Domestic",      "country": "US",             "status": "approved"},
    {"txn_id": "card_004", "time": "15:48", "merchant": "Apple Store Orchard Rd",  "amount": 4500.00, "locale": "International",  "country": "Singapore",      "status": "approved"},
]

# The pending high-value international authorization that triggers the council review.
CARD_SURGE_TRIGGER: Dict[str, Any] = {
    "txn_id": "card_pending", "time": "17:22", "merchant": "TechMegastore Singapore",
    "amount": 12000.00, "locale": "International", "country": "Singapore", "status": "pending_review",
}


def card_surge_summary() -> Dict[str, Any]:
    """Day-to-date card surge stats — used by the council agents and Human Review evidence."""
    return {
        "card_last4":   CARD_LAST4,
        "today_count":  len(CARD_TRANSACTIONS),
        "today_total":  round(sum(t["amount"] for t in CARD_TRANSACTIONS), 2),
        "intl_count":   sum(1 for t in CARD_TRANSACTIONS if t["locale"] == "International"),
        "pending":      CARD_SURGE_TRIGGER,
        "transactions": CARD_TRANSACTIONS,
    }


# ── Transfer Ledger ───────────────────────────────────────────────────────────
# Populated at runtime as transfers are executed during the demo session.

TRANSFER_LEDGER: List[Dict[str, Any]] = []

# ── Reset helper ──────────────────────────────────────────────────────────────
# Canonical initial balances — used by the demo reset endpoint to restore
# account state without restarting the mybank server process.

_INITIAL_BALANCES: Dict[str, Dict[str, float]] = {
    "checking": {"balance": 45_200.00, "available_balance": 45_200.00},
    "savings":  {"balance": 1_247_500.00, "available_balance": 1_247_500.00},
}

# Snapshot of the seed transaction history, captured at import. reset_accounts()
# restores TRANSACTIONS to this so session transfers (added by record_transaction)
# are cleared between demo runs — without restarting the server.
import copy as _copy
_INITIAL_TRANSACTIONS: Dict[str, List[Dict[str, Any]]] = _copy.deepcopy(TRANSACTIONS)

# Rolling cap: keep the most recent N transactions per account so the list
# rotates rather than growing unbounded across a long demo session.
_TXN_CAP = 20


def record_transaction(
    account_key: str,
    txn_type: str,
    description: str,
    amount: float,
    category: str = "Transfer",
) -> None:
    """
    Prepend a transaction to an account's history so session activity (e.g. a
    domestic/international transfer) shows up in 'recent transactions'. The list
    is newest-first and capped at _TXN_CAP (rotating). balance_after is set to the
    account's CURRENT balance — callers deduct/credit the balance BEFORE calling
    this. (get_recent_transactions also recomputes balance_after dynamically, so
    the figure stays coherent regardless.)
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
    """Restore account balances + transaction history to initial demo values and
    clear the transfer ledger (called by the demo /api/reset endpoint)."""
    for acct_key, initial in _INITIAL_BALANCES.items():
        if acct_key in ACCOUNTS:
            ACCOUNTS[acct_key].update(initial)
    # Restore the seed transaction history (drops session transfers)
    TRANSACTIONS.clear()
    TRANSACTIONS.update(_copy.deepcopy(_INITIAL_TRANSACTIONS))
    TRANSFER_LEDGER.clear()


# ── Credit Card Catalog — Story 4 ──────────────────────────────────────────────
# 5 cards with distinct international travel profiles.
# Engineered conflict: criteria-match ranking ≠ suitability ranking.
#   GlobalElite Platinum: best raw features (criteria top pick)
#   TravelPrime Visa:     best value for this customer's profile (suitability top pick)

CREDIT_CARDS: Dict[str, Dict[str, Any]] = {
    "global_elite_platinum": {
        "card_id":          "global_elite_platinum",
        "name":             "GlobalElite Platinum",
        "issuer":           "MyBank",
        "country_coverage": 140,
        "forex_fee":        "0.0%",
        "rewards_rate":     "3x airline miles on travel, 2x on dining",
        "annual_fee":       "$595",
        "income_minimum":   150_000,
        "credit_score_min": 750,
        "lounge_access":    True,
        "travel_insurance": True,
        "highlights":       "Premium tier · 140-country acceptance · 0% FX · Priority Pass lounge",
    },
    "travel_prime_visa": {
        "card_id":          "travel_prime_visa",
        "name":             "TravelPrime Visa",
        "issuer":           "MyBank",
        "country_coverage": 95,
        "forex_fee":        "0.5%",
        "rewards_rate":     "2x miles on all purchases abroad, 1.5x domestic",
        "annual_fee":       "$295",
        "income_minimum":   80_000,
        "credit_score_min": 700,
        "lounge_access":    True,
        "travel_insurance": True,
        "highlights":       "Best value for frequent travelers · 95 countries · flexible miles · lounge",
    },
    "world_miles_mastercard": {
        "card_id":          "world_miles_mastercard",
        "name":             "WorldMiles Mastercard",
        "issuer":           "MyBank",
        "country_coverage": 65,
        "forex_fee":        "1.5%",
        "rewards_rate":     "2.5x miles on travel, 1x elsewhere",
        "annual_fee":       "$150",
        "income_minimum":   60_000,
        "credit_score_min": 680,
        "lounge_access":    False,
        "travel_insurance": True,
        "highlights":       "High miles multiplier · moderate fee · strong on travel spend",
    },
    "everyday_rewards_gold": {
        "card_id":          "everyday_rewards_gold",
        "name":             "EverydayRewards Gold",
        "issuer":           "MyBank",
        "country_coverage": 45,
        "forex_fee":        "2.5%",
        "rewards_rate":     "1.5x points on all purchases",
        "annual_fee":       "$95",
        "income_minimum":   40_000,
        "credit_score_min": 650,
        "lounge_access":    False,
        "travel_insurance": False,
        "highlights":       "Low annual fee · simple cashback rewards · limited international reach",
    },
    "standard_card_blue": {
        "card_id":          "standard_card_blue",
        "name":             "StandardCard Blue",
        "issuer":           "MyBank",
        "country_coverage": 30,
        "forex_fee":        "3.0%",
        "rewards_rate":     "0.5x points on all purchases",
        "annual_fee":       "$0",
        "income_minimum":   25_000,
        "credit_score_min": 620,
        "lounge_access":    False,
        "travel_insurance": False,
        "highlights":       "No annual fee · basic worldwide acceptance · entry-level card",
    },
}


# ── Customer Financial Profile — Story 4 ──────────────────────────────────────
# Supplements CUSTOMER for card suitability scoring.
# Conflict: GlobalElite has best features, but TravelPrime is better VALUE
# for this customer's income bracket and travel pattern (14 trips/year, $245k).

CUSTOMER_FINANCIAL: Dict[str, Any] = {
    "annual_income":    245_000,
    "net_worth":        1_500_000,
    "credit_score":     785,
    "travel_frequency": "frequent",
    "trips_per_year":   14,
    "primary_regions":  ["Europe", "Latin America", "Asia-Pacific"],
    "existing_cards":   ["standard_card_blue"],
    "risk_profile":     "moderate",
    "account_tier":     "premium",
}
