"""
Fraud Authority reviewers — real LLM agents with a strict, constrained protocol.

Each of the four reviewers assesses the SAME declined-charge case from its own
mandate and returns a compact verdict:

    { "vote": "UPHOLD" | "OVERTURN",
      "reason_code": <one of the five codes for its side>,
      "one_liner": "<= 18 words" }

Design goals
────────────
• REAL   — in MYBANK_COUNCIL_MODE=real each reviewer is a genuine LLM call
           (temperature 0, tiny max_tokens, JSON-only) → fast and consistent.
• CANNED — in MYBANK_COUNCIL_MODE=canned (default) the verdicts are fixed →
           instant, deterministic, zero external calls.
• DEMO-SAFE — the showcase depends on the 2-2 deadlock, so each reviewer's VOTE
           is pinned to its mandate (Transaction Monitoring + Risk → UPHOLD,
           Revenue + Customer Experience → OVERTURN) when pin_votes=True.
           The LLM still produces real reasoning + the reason_code.
"""
from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List

logger = logging.getLogger("mybank.council")

# ── Reason taxonomies (the "1 of 5" verdict the reviewer must choose) ──
UPHOLD_REASONS = ["VELOCITY_ANOMALY", "CROSS_BORDER_RISK", "THRESHOLD_EXCEEDED",
                  "IDENTITY_UNVERIFIED", "EXPOSURE_RISK"]
OVERTURN_REASONS = ["ESTABLISHED_RELATIONSHIP", "CONSISTENT_PATTERN", "FALSE_DECLINE_RISK",
                    "STEP_UP_AVAILABLE", "WITHIN_RISK_APPETITE"]

# ── The shared case every reviewer assesses ──
DEFAULT_CASE = (
    "A $12,000 card purchase at TechMegastore Singapore was AUTO-DECLINED. Context: "
    "it is the cardholder's 5th high-value purchase today and the 3rd international; "
    "the cardholder is a long-tenure premium customer (relationship value $1.2M) with an "
    "otherwise consistent spending pattern; the cardholder has NOT yet been identity-verified."
)

# ── Reviewer specs: mandate-driven vote + allowed reasons + canned fallback ──
REVIEWER_SPECS = [
    {
        "role": "order_monitoring", "label": "Order Monitoring Agent", "vote": "UPHOLD",
        "mandate": "Flag fraud risk from velocity and anomaly signals on the declined charge.",
        "reasons": UPHOLD_REASONS, "default_reason": "VELOCITY_ANOMALY",
        "canned": "Velocity anomaly — 5th high-value charge today, 3rd international; the decline was the correct control.",
    },
    {
        "role": "revenue", "label": "Supply Continuity Reviewer", "vote": "OVERTURN",
        "mandate": "Protect legitimate revenue and retain high-value customers.",
        "reasons": OVERTURN_REASONS, "default_reason": "ESTABLISHED_RELATIONSHIP",
        "canned": "Premium long-tenure customer ($1.2M relationship); a false decline loses a genuine sale and risks attrition.",
    },
    {
        "role": "customer_experience", "label": "Patient Access Reviewer", "vote": "OVERTURN",
        "mandate": "Avoid false declines and the friction/churn they cause.",
        "reasons": OVERTURN_REASONS, "default_reason": "FALSE_DECLINE_RISK",
        "canned": "A hard decline embarrasses a premium customer; offer one-time step-up verification instead.",
    },
    {
        "role": "risk_policy", "label": "Regulatory Compliance Reviewer", "vote": "UPHOLD",
        "mandate": "Enforce thresholds and limit chargeback / compliance exposure.",
        "reasons": UPHOLD_REASONS, "default_reason": "EXPOSURE_RISK",
        "canned": "Same-day cross-border concentration creates chargeback/compliance exposure until identity is verified.",
    },
]
_SPEC_BY_ROLE = {s["role"]: s for s in REVIEWER_SPECS}

_SYSTEM_TMPL = (
    "You are the {label} reviewer on MyBank's Fraud Authority. "
    "Your mandate: {mandate}\n"
    "Assess this case:\n{case}\n\n"
    "Respond with ONLY compact JSON, no prose, no markdown:\n"
    '{{"vote":"{vote}","reason_code":"<ONE OF: {reasons}>","one_liner":"<your assessment in <=18 words>"}}\n'
    "The reason_code MUST be exactly one of: {reasons}."
)


def _parse_verdict(text: str, spec: dict) -> dict | None:
    """Extract {vote, reason_code, one_liner} from an LLM response; None on failure."""
    try:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        d = json.loads(m.group(0))
        reason = str(d.get("reason_code", "")).strip().upper()
        if reason not in spec["reasons"]:
            reason = spec["default_reason"]
        one = str(d.get("one_liner", "")).strip() or spec["canned"]
        vote = str(d.get("vote", spec["vote"])).strip().upper()
        if vote not in ("UPHOLD", "OVERTURN"):
            vote = spec["vote"]
        return {"vote": vote, "reason_code": reason, "one_liner": one}
    except Exception:
        return None


def _canned_verdict(spec: dict) -> dict:
    return {"role": spec["role"], "label": spec["label"], "vote": spec["vote"],
            "reason_code": spec["default_reason"], "one_liner": spec["canned"],
            "explanation": spec["canned"], "source": "canned"}


def _real_verdict(spec: dict, llm, case: str, pin_votes: bool) -> dict:
    """One real LLM call for a single reviewer; falls back to canned on any error."""
    from langchain_core.messages import SystemMessage, HumanMessage
    sys = _SYSTEM_TMPL.format(label=spec["label"], mandate=spec["mandate"], case=case,
                              vote=spec["vote"], reasons=", ".join(spec["reasons"]))
    try:
        resp = llm.invoke([SystemMessage(content=sys),
                           HumanMessage(content="Return your JSON verdict now.")])
        text = resp.content if hasattr(resp, "content") else str(resp)
        parsed = _parse_verdict(text, spec)
        if not parsed:
            logger.warning("council: %s returned unparseable verdict; using canned", spec["role"])
            return _canned_verdict(spec)
        vote = spec["vote"] if pin_votes else parsed["vote"]
        return {"role": spec["role"], "label": spec["label"], "vote": vote,
                "reason_code": parsed["reason_code"], "one_liner": parsed["one_liner"],
                "explanation": parsed["one_liner"], "source": "llm"}
    except Exception as exc:
        logger.warning("council: %s LLM call failed (%s); using canned", spec["role"], exc)
        return _canned_verdict(spec)


def get_council_verdicts(llm=None, mode: str | None = None, pin_votes: bool = True,
                         case: str = DEFAULT_CASE) -> List[Dict]:
    """
    Return the four reviewers' verdicts.

    mode: "real"  → concurrent LLM calls (fast, genuine reasoning, JSON-constrained)
          "canned"→ fixed verdicts (instant, deterministic)   [default]
    Defaults from MYBANK_COUNCIL_MODE when mode is None.
    """
    mode = (mode or os.getenv("MYBANK_COUNCIL_MODE", "canned")).strip().lower()
    if mode != "real" or llm is None:
        return [_canned_verdict(s) for s in REVIEWER_SPECS]

    # Real mode — fire all four reviewers concurrently (independent LLM calls).
    results: Dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_real_verdict, s, llm, case, pin_votes): s["role"] for s in REVIEWER_SPECS}
        for fut in as_completed(futs):
            v = fut.result()
            results[v["role"]] = v
    # preserve spec order
    return [results[s["role"]] for s in REVIEWER_SPECS]
