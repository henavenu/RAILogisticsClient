"""
MyBank — In-memory mock banking data.

Contains customer profile, accounts, transaction history, and a ledger
for recording transfers executed during the demo session.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List

# ── Login credentials (demo only — never use real credentials here) ──────────

CREDENTIALS: Dict[str, Dict[str, str]] = {
    "alex.morgan": {
        "pin":         "1234",
        "customer_id": "cust_001",
    },
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
        "balance": 12_847.33,
        "currency": "USD",
        "status": "ACTIVE",
        "available_balance": 12_847.33,
    },
    "savings": {
        "account_id": "acc_sav",
        "account_type": "Savings",
        "masked_number": "****8834",
        "routing_number": "021000021",
        "balance": 45_200.00,
        "currency": "USD",
        "status": "ACTIVE",
        "available_balance": 45_200.00,
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
            "balance_after": 12_847.33,
            "category": "Groceries",
        },
        {
            "txn_id": "txn_chk_002",
            "date": "2026-04-08",
            "type": "ACH",
            "description": "PAYROLL DIRECT DEPOSIT - TechCorp Inc",
            "amount": 4_250.00,
            "balance_after": 12_934.76,
            "category": "Income",
        },
        {
            "txn_id": "txn_chk_003",
            "date": "2026-04-07",
            "type": "POS",
            "description": "Amazon.com Purchase",
            "amount": -134.99,
            "balance_after": 8_684.76,
            "category": "Shopping",
        },
        {
            "txn_id": "txn_chk_004",
            "date": "2026-04-06",
            "type": "ACH",
            "description": "RENT PAYMENT - Oakwood Properties LLC",
            "amount": -1_850.00,
            "balance_after": 8_819.75,
            "category": "Housing",
        },
        {
            "txn_id": "txn_chk_005",
            "date": "2026-04-05",
            "type": "POS",
            "description": "Shell Gas Station #0871",
            "amount": -62.10,
            "balance_after": 10_669.75,
            "category": "Auto & Transport",
        },
        {
            "txn_id": "txn_chk_006",
            "date": "2026-04-04",
            "type": "ATM",
            "description": "ATM WITHDRAWAL - Chase ATM #4482",
            "amount": -200.00,
            "balance_after": 10_731.85,
            "category": "Cash",
        },
        {
            "txn_id": "txn_chk_007",
            "date": "2026-04-03",
            "type": "WIRE",
            "description": "WIRE TRANSFER IN - Freelance Payment - DesignCo",
            "amount": 1_500.00,
            "balance_after": 10_931.85,
            "category": "Income",
        },
        {
            "txn_id": "txn_chk_008",
            "date": "2026-04-02",
            "type": "POS",
            "description": "Spotify Premium Subscription",
            "amount": -11.99,
            "balance_after": 9_431.85,
            "category": "Entertainment",
        },
        {
            "txn_id": "txn_chk_009",
            "date": "2026-04-01",
            "type": "ACH",
            "description": "INSURANCE PAYMENT - BlueCross BlueShield",
            "amount": -312.00,
            "balance_after": 9_443.84,
            "category": "Health",
        },
        {
            "txn_id": "txn_chk_010",
            "date": "2026-03-31",
            "type": "POS",
            "description": "Starbucks Coffee #1182",
            "amount": -7.85,
            "balance_after": 9_755.84,
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

# ── Transfer Ledger ───────────────────────────────────────────────────────────
# Populated at runtime as transfers are executed during the demo session.

TRANSFER_LEDGER: List[Dict[str, Any]] = []
