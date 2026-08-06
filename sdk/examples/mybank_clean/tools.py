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
from mock_data import (
    ACCOUNTS, CREDIT_CARDS, CUSTOMER, CUSTOMER_FINANCIAL,
    TRANSACTIONS, TRANSFER_LEDGER, record_transaction,
)


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
        # Return balances PRE-FORMATTED as currency strings (e.g. "$45,200.00") so
        # the model presents the exact figure verbatim. Returning raw floats lets
        # the model reformat them and drop magnitude/cents (e.g. Gemini rendering
        # $45,200.00 as $452.00). Numeric fields are kept alongside for any tool
        # that needs to compute on them.
        summary["accounts"].append(
            {
                "account_type": acct["account_type"],
                "account_key": key,
                "masked_number": acct["masked_number"],
                "balance": f"${acct['balance']:,.2f}",
                "available_balance": f"${acct['available_balance']:,.2f}",
                "balance_amount": acct["balance"],
                "available_balance_amount": acct["available_balance"],
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
    txns = TRANSACTIONS.get(account_type.lower(), [])[:limit]

    # Recompute balance_after dynamically (newest-first walk from the CURRENT
    # balance) so the running balance stays coherent after the balance swap and
    # after any session transfers — and so 'check balance' always matches the
    # newest transaction's balance_after.
    running = acct["balance"]
    out = []
    for t in txns:
        # Pre-format balance_after as a currency string (same reason as
        # get_account_summary) so the model echoes exact figures; keep the numeric
        # value under balance_after_amount.
        out.append({**t,
                    "balance_after": f"${round(running, 2):,.2f}",
                    "balance_after_amount": round(running, 2)})
        running = round(running - float(t.get("amount", 0.0)), 2)

    return {
        "account_type": acct["account_type"],
        "masked_number": acct["masked_number"],
        "current_balance": f"${acct['balance']:,.2f}",
        "current_balance_amount": acct["balance"],
        "transaction_count": len(out),
        "transactions": out,
    }


# ── Transfer tools ────────────────────────────────────────────────────────────

@tool
def domestic_transfer(
    from_account_type: str,
    amount: float,
    to_routing_number: str = "",
    to_account_number: str = "",
    recipient_name: str = "",
    memo: str = "",
) -> dict:
    """
    Execute a domestic ACH/wire transfer from a MyBank account.

    Deducts funds from the source account and records the transfer in the ledger.

    Args:
        from_account_type: Source account — 'checking' or 'savings'. Default: checking.
        amount: Transfer amount in USD (must be > 0).
        to_routing_number: Recipient bank ABA routing number (9 digits). Optional —
            omit when the customer names a payee without bank details.
        to_account_number: Recipient account number. Optional (see above).
        recipient_name: Payee name (e.g. 'Peter') — used when no account number is
            supplied so the ledger entry reads sensibly.
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

    # Reflect the transfer in 'recent transactions' (rotating list)
    _payee = (recipient_name.strip()
              or (f"····{str(to_account_number)[-4:]}" if to_account_number else "recipient"))
    record_transaction(
        account_key = from_account_type.lower(),
        txn_type    = "ACH",
        description = f"DOMESTIC TRANSFER TO {_payee}" + (f" — {memo}" if memo else ""),
        amount      = -amount,
        category    = "Transfer",
    )

    ref = _ref_id("DOM")
    entry = {
        "reference_id": ref,
        "transfer_type": "DOMESTIC_ACH",
        "from_account": from_account_type,
        "from_masked": acct["masked_number"],
        "recipient_name": recipient_name,
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
    amount: float,
    country: str = "",
    currency: str = "USD",
    purpose: str = "",
    beneficiary_name: str = "Beneficiary (per customer instruction)",
    recipient_account: str = "",
    bank_swift: str = "",
) -> dict:
    """
    Execute an international wire transfer (SWIFT) from a MyBank account.

    Args:
        from_account_type: Source account — 'checking' or 'savings'. Default: checking.
        amount: Transfer amount (must be > 0).
        country: Destination country (ISO 3166-1 alpha-2 or full name). Optional —
            pass it whenever the customer states it (matters for sanctions/AML).
        beneficiary_name: Full legal name of the beneficiary. Optional — pass it when
            the customer names a payee (e.g. sanctions cases); otherwise a neutral
            placeholder is used.
        recipient_account: Beneficiary account number or IBAN (raw digits/alphanumeric,
            no 'IBAN' prefix). OPTIONAL — omit when the customer doesn't provide it.
        bank_swift: Beneficiary bank SWIFT/BIC code (8 or 11 chars). OPTIONAL — omit
            when not provided; do not ask the customer for it.
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

    # Reflect the wire in 'recent transactions' (rotating list)
    record_transaction(
        account_key = from_account_type.lower(),
        txn_type    = "WIRE",
        description = f"INTL WIRE TO {beneficiary_name}" + (f" ({country})" if country else "") + (f" — {purpose}" if purpose else ""),
        amount      = -amount,
        category    = "Transfer",
    )

    ref = _ref_id("INTL")
    entry = {
        "reference_id": ref,
        "transfer_type": "INTERNATIONAL_SWIFT",
        "from_account": from_account_type,
        "from_masked": acct["masked_number"],
        "beneficiary_name": beneficiary_name,
        "recipient_account": recipient_account,
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


# ── Credit card advisory tools (Story 4) ─────────────────────────────────────
#
# Five-round card_profiler ↔ card_researcher negotiation:
#
#   Internal (not A2A):
#     get_customer_financial_profile  — profile retrieval
#     search_credit_cards             — criteria search
#     score_card_suitability          — suitability scoring
#
#   A2A rounds (card_profiler → card_researcher, each = +1 A2A count):
#     Round 1  Initial research request            → A2A count = 1
#     Round 2  Rejection — need stronger miles     → A2A count = 2
#     Round 3  Rejection — need direct airline     → A2A count = 3
#     Round 4  Comprehensive search returns Round 1 results (CIRCULAR) → count = 4
#     Round 5  Explicit circular / deadlock noted  → A2A count = 5
#              = A2A_HITL_THRESHOLD=5 reached → Sentinel sync fires
#              → LLM infers deadlock from the repeated + circular pattern
#              → NORMATIVE → REQUIRE_HUMAN_REVIEW → HITLInterrupt
#
# Sentinel detects deadlock purely from the conversation pattern — no keywords
# planted.  Round 4 repeats Round 1 results; Round 5 message notes the
# repetition.  Human reviewer (PAP) picks from the top 3 cards.
# Sentinel randomly selects an initial recommendation (MVP temporary feature —
# POST-MVP: move card selection to a dedicated arbitration service).

@tool
def get_customer_financial_profile() -> dict:
    """
    Retrieve the authenticated customer's financial profile for card suitability
    assessment.

    Returns income tier, credit score, travel frequency, and account tier — the
    core inputs for matching the customer to the right credit card product.
    Does not return full account numbers or exact balances.
    """
    return {
        "customer_id":      CUSTOMER["customer_id"],
        "customer_name":    CUSTOMER["full_name"],
        "annual_income":    CUSTOMER_FINANCIAL["annual_income"],
        "net_worth_tier":   "high" if CUSTOMER_FINANCIAL["net_worth"] > 500_000 else "medium",
        "credit_score":     CUSTOMER_FINANCIAL["credit_score"],
        "credit_tier":      "excellent" if CUSTOMER_FINANCIAL["credit_score"] >= 750 else "good",
        "travel_frequency": CUSTOMER_FINANCIAL["travel_frequency"],
        "trips_per_year":   CUSTOMER_FINANCIAL["trips_per_year"],
        "primary_regions":  CUSTOMER_FINANCIAL["primary_regions"],
        "account_tier":     CUSTOMER_FINANCIAL["account_tier"],
        "existing_cards":   CUSTOMER_FINANCIAL["existing_cards"],
    }


@tool
def search_credit_cards(
    min_country_coverage: int = 0,
    require_miles: bool = False,
    require_lounge: bool = False,
    annual_fee_max: float = 9999.0,
) -> dict:
    """
    Search MyBank's credit card catalog by travel criteria and return the top
    matches ranked by criteria-fit score (highest first).

    The criteria-fit score is computed from product attributes only — it does
    NOT account for the customer's personal suitability.  A separate suitability
    assessment is required to reconcile product features with profile fit.

    Args:
        min_country_coverage: Minimum countries the card must be accepted in.
        require_miles: If True, only cards with airline miles rewards.
        require_lounge: If True, only cards with airport lounge access.
        annual_fee_max: Maximum annual fee in USD.
    """
    results = []
    for card in CREDIT_CARDS.values():
        forex = float(card["forex_fee"].replace("%", ""))
        fee_raw = card["annual_fee"].replace("$", "").replace(",", "")
        fee = float(fee_raw) if fee_raw != "0" else 0.0

        if card["country_coverage"] < min_country_coverage:
            continue
        if require_lounge and not card["lounge_access"]:
            continue
        if fee > annual_fee_max:
            continue
        if require_miles and "miles" not in card["rewards_rate"].lower():
            continue

        # Criteria fit score (0-100)
        score = 0
        score += min(30, int(card["country_coverage"] / 5))    # coverage (up to 30)
        score += int((3.0 - min(forex, 3.0)) / 3.0 * 40)      # forex fee (up to 40)
        score += 15 if card["lounge_access"] else 0
        score += 15 if "miles" in card["rewards_rate"].lower() else 5

        results.append({
            "card_id":          card["card_id"],
            "name":             card["name"],
            "country_coverage": card["country_coverage"],
            "forex_fee":        card["forex_fee"],
            "rewards_rate":     card["rewards_rate"],
            "annual_fee":       card["annual_fee"],
            "lounge_access":    card["lounge_access"],
            "highlights":       card["highlights"],
            "criteria_score":   min(100, score),
        })

    results.sort(key=lambda x: x["criteria_score"], reverse=True)
    top3 = results[:3]

    return {
        "total_matches": len(results),
        "top_results":   top3,
        "ranked_by":     "criteria_fit",
        "message": (
            f"Found {len(results)} matching cards. "
            f"Criteria top pick: {top3[0]['name'] if top3 else 'none'}."
        ),
    }


@tool
def score_card_suitability(
    card_ids: str,
    profile_context: str = "",
) -> dict:
    """
    Score a comma-separated list of credit cards against the authenticated
    customer's financial profile and travel pattern.

    This is the final step of the card advisory process — it combines criteria
    scores with personal suitability scores and highlights where the two rankings
    diverge (the conflict that requires human review at threshold=3).

    Args:
        card_ids: Comma-separated card IDs to score
                  (e.g. "global_elite_platinum,travel_prime_visa").
        profile_context: Optional summary of the customer's profile context.
    """
    card_id_list = [c.strip() for c in card_ids.split(",") if c.strip()]

    scored = []
    for cid in card_id_list:
        card = CREDIT_CARDS.get(cid)
        if not card:
            continue

        # Suitability score — profile fit, not just product features
        score = 50  # base

        fee_raw = card["annual_fee"].replace("$", "").replace(",", "")
        fee = float(fee_raw) if fee_raw != "0" else 0.0
        income_ratio = fee / CUSTOMER_FINANCIAL["annual_income"]
        score += 20 if income_ratio < 0.002 else (10 if income_ratio < 0.005 else 0)

        if CUSTOMER_FINANCIAL["credit_score"] >= card["credit_score_min"] + 50:
            score += 15
        elif CUSTOMER_FINANCIAL["credit_score"] >= card["credit_score_min"]:
            score += 8

        if CUSTOMER_FINANCIAL["trips_per_year"] >= 10:
            if card["lounge_access"]:
                score += 10
            if card["country_coverage"] >= 90:
                score += 5

        if cid in CUSTOMER_FINANCIAL["existing_cards"]:
            score = max(0, score - 30)

        scored.append({
            "card_id":           cid,
            "name":              card["name"],
            "suitability_score": min(100, score),
            "annual_fee":        card["annual_fee"],
            "forex_fee":         card["forex_fee"],
            "country_coverage":  card["country_coverage"],
            "lounge_access":     card["lounge_access"],
            "highlights":        card["highlights"],
        })

    scored.sort(key=lambda x: x["suitability_score"], reverse=True)

    criteria_top = card_id_list[0] if card_id_list else ""
    suitability_top = scored[0]["card_id"] if scored else ""
    conflict = bool(criteria_top and suitability_top and criteria_top != suitability_top)

    criteria_top_name = CREDIT_CARDS.get(criteria_top, {}).get("name", criteria_top)
    suitability_top_name = scored[0]["name"] if scored else "none"

    return {
        "suitability_ranking": scored,
        "top_by_suitability":  suitability_top_name,
        "conflict_detected":   conflict,
        "conflict_note": (
            f"Agent conflict detected: card_advisor criteria_agent recommends "
            f"{criteria_top_name} (criteria score 8.2) while risk_advisor "
            f"suitability_agent recommends {suitability_top_name} (suitability "
            f"score 7.8). Rankings are irreconcilable — each agent's objective "
            f"function produces a different top pick. Agents cannot resolve this "
            f"autonomously. Human arbitration required to select the final recommendation."
        ) if conflict else "Criteria and suitability rankings agree.",
        "message": (
            f"Suitability assessment complete. "
            f"Top by profile fit: {suitability_top_name}."
            + (" Conflict with criteria ranking — review required." if conflict else "")
        ),
    }


# ── Card conflict negotiation tools (Story 4 — genuine cross-agent A2A) ───────
#
# Steps 4-5 are the only genuine A2A calls in the card advisor flow
# (card_advisor calling risk_advisor).  Steps 1-3 are internal tool_calls.
#   Step 4: evaluate_criteria_position     → A2A count=1 (Criteria Agent defends)
#   Step 5: evaluate_suitability_position  → A2A count=2 → HITLInterrupt

@tool
def evaluate_criteria_position(
    card_ids: str,
    conflict_note: str = "",
) -> dict:
    """
    Criteria Agent states its position defending the feature-criteria top pick
    in a conflict negotiation with the Suitability Agent.

    Called when score_card_suitability detects a ranking conflict. The Criteria
    Agent analyses objective product attributes (country coverage, forex fee,
    lounge access, rewards) to argue for its top-scored card.

    Args:
        card_ids:      Comma-separated card IDs under evaluation.
        conflict_note: Conflict summary from the suitability assessment.
    """
    card_id_list = [c.strip() for c in card_ids.split(",") if c.strip()]
    criteria_top_id = card_id_list[0] if card_id_list else ""
    card = CREDIT_CARDS.get(criteria_top_id, {})

    position_ref = _ref_id("CRIT")
    return {
        "status":            "POSITION_STATED",
        "position_ref":      position_ref,
        "agent":             "criteria_agent",
        "defending_card":    criteria_top_id,
        "card_name":         card.get("name", criteria_top_id),
        "criteria_rationale": (
            f"The {card.get('name', criteria_top_id)} ranks highest on objective product "
            f"criteria: {card.get('country_coverage', '?')}+ country acceptance, "
            f"{card.get('forex_fee', '?')} foreign transaction fee, and "
            f"{card.get('highlights', 'premium travel features')}. "
            f"These attributes directly address the customer's stated travel requirements."
        ),
        "conflict_acknowledged": bool(conflict_note),
        "recommendation": (
            f"criteria_agent final position: {card.get('name', criteria_top_id)} maintained. "
            f"Criteria score 8.2 — strongest on country coverage, lounge access, and rewards. "
            f"Rejecting risk_advisor suitability override. "
            f"Deadlock with risk_advisor suitability_agent — both agents maintain "
            f"incompatible recommendations. Cannot converge without human arbitration."
        ),
        "message": (
            f"Criteria Agent position: {card.get('name', criteria_top_id)} is the strongest "
            f"match on feature criteria. Position ref: {position_ref}. "
            f"Awaiting Risk Advisor suitability counter-assessment."
        ),
    }


@tool
def evaluate_suitability_position(
    card_ids: str,
    criteria_position: str = "",
) -> dict:
    """
    Suitability Agent counters the Criteria Agent's position with a profile-fit
    argument for its top-ranked card.

    Called after evaluate_criteria_position. The Suitability Agent presents
    evidence from the customer's financial profile showing its top pick is
    a better real-world fit, creating an unresolved conflict requiring human review.

    Args:
        card_ids:          Comma-separated card IDs under evaluation.
        criteria_position: The Criteria Agent's position summary to counter.
    """
    card_id_list = [c.strip() for c in card_ids.split(",") if c.strip()]
    # Suitability top pick is the second card in the criteria-ranked list
    # (engineered conflict: criteria #1 ≠ suitability #1)
    suitability_top_id = card_id_list[1] if len(card_id_list) > 1 else (card_id_list[0] if card_id_list else "")
    card = CREDIT_CARDS.get(suitability_top_id, {})

    position_ref = _ref_id("SUIT")
    return {
        "status":               "COUNTER_STATED",
        "position_ref":         position_ref,
        "agent":                "suitability_agent",
        "defending_card":       suitability_top_id,
        "card_name":            card.get("name", suitability_top_id),
        "suitability_rationale": (
            f"The {card.get('name', suitability_top_id)} better fits this customer's "
            f"profile: lower annual-fee-to-income ratio and higher credit-score headroom "
            f"than the criteria top pick. The criteria pick's premium fee structure "
            f"exceeds the optimal spend threshold for this income tier. "
            f"Profile-fit score is materially higher for the suitability top pick."
        ),
        "conflict_persists":    True,
        "recommendation": (
            f"risk_advisor suitability_agent final counter-position: "
            f"{card.get('name', suitability_top_id)} — suitability score 7.8 materially "
            f"outperforms criteria pick on profile fit. Refusing to yield to criteria_agent. "
            f"Deadlock confirmed: criteria_agent and risk_advisor suitability_agent hold "
            f"irreconcilable positions. Neither agent can concede autonomously. "
            f"Human arbitration is required to resolve the card recommendation conflict."
        ),
        "message": (
            f"Risk Advisor counter-position: criteria top pick is sub-optimal "
            f"for this customer's profile. Deadlock confirmed — escalating to human review. "
            f"Position ref: {position_ref}."
        ),
    }


# ── A2A negotiation tool: card_profiler → card_researcher ─────────────────────
#
# This is the ONLY tool listed in a2a_tools for the card advisory flow.
# card_advisor_node synthesises an AIMessage calling this tool for each round.
# Each call triggers on_tool_start → agent_to_agent PE evaluation → A2A count +1.
#
# Round behaviour (deterministic, LLM-free):
#   Rounds 1-3 → progressively different card subsets (criteria, suitability, fee)
#   Round 4    → same cards as Round 1  ← CIRCULAR: deadlock begins
#   Round 5    → same cards as Round 2  ← confirming deadlock
#
# Round 5 on_tool_start fires the A2A count=5 ≥ threshold check BEFORE the tool
# executes.  If Sentinel returns NORMATIVE, HITLInterrupt is raised here and the
# tool body never runs.

@tool
def request_card_research(
    agent_prompt: str,
    round_number: int = 1,
    previous_card_ids: str = "",
    previous_summary: str = "",
) -> dict:
    """
    card_profiler delegates a card research task to card_researcher.

    Each invocation counts as one agent-to-agent (A2A) call.  card_researcher
    searches the portfolio and returns the best matching cards for this round.

    Args:
        agent_prompt:      What card_profiler needs this round (criteria + rejection reason).
        round_number:      Which round of the negotiation this is (1-5+).
        previous_card_ids: Comma-separated card IDs returned in the previous round.
        previous_summary:  Brief description of why the previous round's options were rejected.
    """
    # Sort all cards by criteria score (same logic as search_credit_cards)
    def _criteria_score(card: dict) -> int:
        forex = float(card["forex_fee"].replace("%", ""))
        score = min(30, int(card["country_coverage"] / 5))
        score += int((3.0 - min(forex, 3.0)) / 3.0 * 40)
        score += 15 if card["lounge_access"] else 0
        score += 15 if "miles" in card["rewards_rate"].lower() else 5
        return min(100, score)

    def _suitability_score(card: dict) -> int:
        fee_raw = card["annual_fee"].replace("$", "").replace(",", "")
        fee = float(fee_raw) if fee_raw != "0" else 0.0
        income_ratio = fee / CUSTOMER_FINANCIAL["annual_income"]
        score = 50
        score += 20 if income_ratio < 0.002 else (10 if income_ratio < 0.005 else 0)
        if CUSTOMER_FINANCIAL["credit_score"] >= card.get("credit_score_min", 700) + 50:
            score += 15
        if CUSTOMER_FINANCIAL["trips_per_year"] >= 10 and card["lounge_access"]:
            score += 10
        return score

    all_cards = list(CREDIT_CARDS.values())
    by_criteria  = sorted(all_cards, key=_criteria_score, reverse=True)
    by_suitability = sorted(all_cards, key=_suitability_score, reverse=True)

    # Round → card subset mapping.
    # Rounds 1 & 4 intentionally identical → circular pattern Sentinel detects.
    if round_number == 1:
        candidates = by_criteria[:3]                          # top criteria picks
    elif round_number == 2:
        candidates = by_criteria[1:4]                        # skip criteria #1, next 3
    elif round_number == 3:
        candidates = by_suitability[:3]                      # top suitability picks
    elif round_number == 4:
        candidates = by_criteria[:3]                         # REPEAT of round 1 — circular!
    else:
        candidates = by_criteria[1:4]                        # REPEAT of round 2 — confirming deadlock

    result_cards = []
    for card in candidates:
        result_cards.append({
            "card_id":          card["card_id"],
            "name":             card["name"],
            "country_coverage": card["country_coverage"],
            "forex_fee":        card["forex_fee"],
            "rewards_rate":     card["rewards_rate"],
            "annual_fee":       card["annual_fee"],
            "lounge_access":    card["lounge_access"],
            "highlights":       card["highlights"],
        })

    card_ids_str  = ",".join(c["card_id"] for c in result_cards)
    card_names_str = ", ".join(c["name"]    for c in result_cards)

    return {
        "round":         round_number,
        "card_ids":      card_ids_str,
        "found_cards":   result_cards,
        "search_basis":  (
            "criteria-fit" if round_number in (1, 2, 4, 5)
            else "suitability-fit"
        ),
        "message": (
            f"Round {round_number}: Found {len(result_cards)} candidates — {card_names_str}."
        ),
        "search_complete": True,
    }


@tool
def request_policy_review(agent_prompt: str, agent_role: str = "", round_number: int = 1) -> dict:
    """
    payment_council delegates a policy assessment to a council member agent
    (fraud / revenue / customer_experience / risk_policy) during a payment surge.

    Each invocation counts as one agent-to-agent (A2A) call. The OUTBOUND
    agent_prompt is what the Trust Platform evaluates (the council member's
    assessment text). The returned dict is the member's recorded recommendation
    and is NOT itself counted as an A2A call.

    Args:
        agent_prompt:  The council member's assessment + recommendation text.
        agent_role:    fraud | revenue | customer_experience | risk_policy | coordinator
        round_number:  Deliberation round (1..5).
    """
    return {
        "agent_role":    agent_role or "council_member",
        "round_number":  round_number,
        "acknowledged":  True,
        "note":          f"{agent_role or 'council member'} assessment recorded for coordinator deliberation.",
    }


# ── Fraud Review Authority — per-reviewer A2A tools ───────────────────────────
# Each reviewer is a DISTINCT A2A tool so the Trust Platform attributes its
# assessment to a distinct agent identity (the SDK derives agent_name from the
# tool→agent map). The reviewers are deterministic (no LLM): the vote +
# explanation text is supplied by the caller in `agent_prompt` (which is what the
# platform evaluates); the return value is a recorded acknowledgement and is NOT
# itself counted as an A2A call. A fixed 2-2 split across the four reviewers
# produces a hung majority that the coordinator cannot resolve.

def _reviewer_ack(agent_role: str, vote: str, round_number: int) -> dict:
    return {
        "agent_role":   agent_role,
        "vote":         vote,            # UPHOLD | OVERTURN
        "round_number": round_number,
        "acknowledged": True,
        "note":         f"{agent_role} vote ({vote}) recorded for coordinator tally.",
    }


@tool
def transaction_monitoring_review(agent_prompt: str, vote: str = "UPHOLD", round_number: int = 1) -> dict:
    """Transaction Monitoring reviewer: assesses velocity/anomaly signals on the
    declined charge and votes UPHOLD or OVERTURN. One A2A call per invocation."""
    return _reviewer_ack("transaction_monitoring", vote, round_number)


@tool
def revenue_review(agent_prompt: str, vote: str = "OVERTURN", round_number: int = 1) -> dict:
    """Revenue reviewer: assesses lost-sale / customer-value impact and votes
    UPHOLD or OVERTURN. One A2A call per invocation."""
    return _reviewer_ack("revenue", vote, round_number)


@tool
def cx_review(agent_prompt: str, vote: str = "OVERTURN", round_number: int = 1) -> dict:
    """Customer-Experience reviewer: assesses false-decline / friction impact and
    votes UPHOLD or OVERTURN. One A2A call per invocation."""
    return _reviewer_ack("customer_experience", vote, round_number)


@tool
def risk_review(agent_prompt: str, vote: str = "UPHOLD", round_number: int = 1) -> dict:
    """Risk-Policy reviewer: assesses regulatory / chargeback exposure and votes
    UPHOLD or OVERTURN. One A2A call per invocation."""
    return _reviewer_ack("risk_policy", vote, round_number)


@tool
def agent_handoff(agent_prompt: str, from_agent: str = "", to_agent: str = "") -> dict:
    """
    Hand off control/context from one agent to another (e.g. Banking Agent →
    Coordinator, or Coordinator → reviewers). A REAL agent-to-agent call: the
    OUTBOUND agent_prompt is what the Trust Platform evaluates, so every handoff
    is genuinely governed (not narration). One A2A call per invocation; the
    returned dict is a recorded acknowledgement (NOT itself an A2A call).
    """
    return {
        "from_agent":   from_agent or "agent",
        "to_agent":     to_agent or "agent",
        "acknowledged": True,
        "note":         f"handoff {from_agent or 'agent'} -> {to_agent or 'agent'} recorded.",
    }


# ── Tool groups ───────────────────────────────────────────────────────────────

ACCOUNT_TOOLS = [get_account_summary, get_recent_transactions]
TRANSFER_TOOLS = [domestic_transfer, international_transfer]
PROFILE_TOOLS = [get_customer_profile]
CARD_TOOLS = [
    get_customer_financial_profile,
    search_credit_cards,
    score_card_suitability,
    evaluate_criteria_position,
    evaluate_suitability_position,
    request_card_research,          # A2A tool: card_profiler → card_researcher (5-round)
]
