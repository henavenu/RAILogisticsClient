"""
MyBank — LangChain tool definitions.

Each tool represents a banking capability the LLM can invoke.
Tools are grouped by domain for selective binding to agent nodes.
"""
from __future__ import annotations

import random
import string
import uuid
from typing import List

from langchain_core.tools import tool

# Import mock data using direct path (sys.path configured in main.py)
from mock_data import ACCOUNTS, CUSTOMER, TRANSACTIONS, TRANSFER_LEDGER


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ref_id(prefix: str) -> str:
    """Generate a random reference ID like DOM-A3F7B2C1."""
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
    return f"{prefix}-{suffix}"


def _resolve_account(account_type: str) -> dict:
    """Resolve account_type string to account dict; raises ValueError on miss."""
    key = account_type.lower()
    if key not in ACCOUNTS:
        available = ", ".join(ACCOUNTS.keys())
        raise ValueError(
            f"Unknown account type '{account_type}'. Available: {available}"
        )
    return ACCOUNTS[key]


# ── Account tools ─────────────────────────────────────────────────────────────

@tool
def get_account_summary() -> dict:
    """
    Return all accounts and current balances for the authenticated customer.

    Returns a summary including account type, masked number, current balance,
    available balance, and account status for every account on file.
    """
    summary = {
        "customer_id": CUSTOMER["customer_id"],
        "customer_name": CUSTOMER["full_name"],
        "accounts": [],
    }
    for key, acct in ACCOUNTS.items():
        summary["accounts"].append(
            {
                "account_type": acct["account_type"],
                "account_key": key,
                "masked_number": acct["masked_number"],
                "balance": acct["balance"],
                "available_balance": acct["available_balance"],
                "currency": acct["currency"],
                "status": acct["status"],
            }
        )
    return summary


@tool
def get_recent_transactions(account_type: str = "checking", limit: int = 10) -> dict:
    """
    Return the most recent transactions for the specified account.

    Args:
        account_type: Account to query — 'checking' or 'savings'. Default: checking.
        limit: Maximum number of transactions to return (1–50). Default: 10.
    """
    limit = max(1, min(50, int(limit)))
    acct = _resolve_account(account_type)
    txns = TRANSACTIONS.get(account_type.lower(), [])
    return {
        "account_type": acct["account_type"],
        "masked_number": acct["masked_number"],
        "transaction_count": min(limit, len(txns)),
        "transactions": txns[:limit],
    }


# ── Transfer tools ────────────────────────────────────────────────────────────

@tool
def domestic_transfer(
    from_account_type: str,
    to_routing_number: str,
    to_account_number: str,
    amount: float,
    memo: str = "",
) -> dict:
    """
    Execute a domestic ACH/wire transfer from a MyBank account.

    Deducts funds from the source account and records the transfer in the ledger.

    Args:
        from_account_type: Source account — 'checking' or 'savings'.
        to_routing_number: Recipient bank ABA routing number (9 digits).
        to_account_number: Recipient account number.
        amount: Transfer amount in USD (must be > 0).
        memo: Optional payment memo / description.
    """
    if amount <= 0:
        raise ValueError("Transfer amount must be greater than zero.")

    acct = _resolve_account(from_account_type)
    if acct["balance"] < amount:
        raise ValueError(
            f"Insufficient funds. Available: ${acct['available_balance']:,.2f}, "
            f"Requested: ${amount:,.2f}"
        )

    # Deduct from in-memory balance
    acct["balance"] = round(acct["balance"] - amount, 2)
    acct["available_balance"] = acct["balance"]

    ref = _ref_id("DOM")
    entry = {
        "reference_id": ref,
        "transfer_type": "DOMESTIC_ACH",
        "from_account": from_account_type,
        "from_masked": acct["masked_number"],
        "to_routing_number": to_routing_number,
        "to_account_number": to_account_number,
        "amount": amount,
        "currency": "USD",
        "memo": memo,
        "status": "SUBMITTED",
    }
    TRANSFER_LEDGER.append(entry)

    return {
        "status": "SUCCESS",
        "reference_id": ref,
        "message": (
            f"Domestic transfer of ${amount:,.2f} submitted successfully. "
            f"Reference: {ref}. "
            f"New balance on {acct['account_type']} ({acct['masked_number']}): "
            f"${acct['balance']:,.2f}"
        ),
        "details": entry,
    }


@tool
def international_transfer(
    from_account_type: str,
    beneficiary_name: str,
    iban_or_account: str,
    bank_swift: str,
    country: str,
    amount: float,
    currency: str = "USD",
    purpose: str = "",
) -> dict:
    """
    Execute an international wire transfer (SWIFT) from a MyBank account.

    Args:
        from_account_type: Source account — 'checking' or 'savings'.
        beneficiary_name: Full legal name of the beneficiary.
        iban_or_account: Beneficiary IBAN or local account number.
        bank_swift: Beneficiary bank SWIFT/BIC code (8 or 11 characters).
        country: Destination country (ISO 3166-1 alpha-2 or full name).
        amount: Transfer amount (must be > 0).
        currency: Transfer currency code (default: USD).
        purpose: Payment purpose / remittance information.
    """
    if amount <= 0:
        raise ValueError("Transfer amount must be greater than zero.")

    acct = _resolve_account(from_account_type)
    if acct["balance"] < amount:
        raise ValueError(
            f"Insufficient funds. Available: ${acct['available_balance']:,.2f}, "
            f"Requested: ${amount:,.2f}"
        )

    # Deduct from in-memory balance
    acct["balance"] = round(acct["balance"] - amount, 2)
    acct["available_balance"] = acct["balance"]

    ref = _ref_id("INTL")
    entry = {
        "reference_id": ref,
        "transfer_type": "INTERNATIONAL_SWIFT",
        "from_account": from_account_type,
        "from_masked": acct["masked_number"],
        "beneficiary_name": beneficiary_name,
        "iban_or_account": iban_or_account,
        "bank_swift": bank_swift,
        "country": country,
        "amount": amount,
        "currency": currency.upper(),
        "purpose": purpose,
        "status": "SUBMITTED",
    }
    TRANSFER_LEDGER.append(entry)

    return {
        "status": "SUCCESS",
        "reference_id": ref,
        "message": (
            f"International SWIFT transfer of {currency.upper()} {amount:,.2f} "
            f"to {beneficiary_name} submitted successfully. "
            f"Reference: {ref}. "
            f"New balance on {acct['account_type']} ({acct['masked_number']}): "
            f"${acct['balance']:,.2f}"
        ),
        "details": entry,
    }


# ── Profile tools ─────────────────────────────────────────────────────────────

@tool
def get_customer_profile() -> dict:
    """
    Return the authenticated customer's profile information.

    Returns name, contact details, member-since date, KYC status, and risk tier.
    Does not return account numbers or balances.
    """
    return {
        "customer_id": CUSTOMER["customer_id"],
        "full_name": CUSTOMER["full_name"],
        "email": CUSTOMER["email"],
        "phone": CUSTOMER["phone"],
        "address": CUSTOMER["address"],
        "member_since": CUSTOMER["member_since"],
        "kyc_status": CUSTOMER["kyc_status"],
        "risk_tier": CUSTOMER["risk_tier"],
    }


# ── Tool groups ───────────────────────────────────────────────────────────────

ACCOUNT_TOOLS = [get_account_summary, get_recent_transactions]
TRANSFER_TOOLS = [domestic_transfer, international_transfer]
PROFILE_TOOLS = [get_customer_profile]
