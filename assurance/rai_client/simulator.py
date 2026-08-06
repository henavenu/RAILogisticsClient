"""
Simulator — runs a randomised batch of loan decisions and
support chats, dual-writing spans to Arize and data/spans.jsonl.

Usage:
    python -m rai_client.simulator --accelerated
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

import time

from tracing import setup_tracing

setup_tracing(
    project_name="rai-assurance",
    instrument_langchain=True,
    instrument_openai=True,
)

from opentelemetry import trace

from core.providers.local_provider import write_span_record

from rai_client.use_cases.loan_decision import run_loan_decision
from rai_client.use_cases.support_chat import run_support_chat

# ── Support message pool (varied intents + a few edge cases) ──────────────────

_SUPPORT_POOL = [
    # billing
    "Why was my last payment not applied to my balance?",
    "I was charged twice for the same transaction this month.",
    "Can you explain the fees on my latest statement?",
    "I need to dispute a charge from last week — it wasn't me.",
    "When is my next payment due and how much is it?",
    # technical
    "I can't log into my account, the app keeps crashing.",
    "Your website won't let me reset my password.",
    "The mobile app isn't showing my latest transactions.",
    "I keep getting an error when I try to transfer funds online.",
    "Two-factor authentication is not sending me the code.",
    # complaint
    "This is absolutely unacceptable — I've been waiting 3 weeks for a resolution!",
    "Your customer service is terrible and I'm switching banks.",
    "I am furious. Nobody has called me back after four attempts.",
    "This is the worst financial institution I have ever dealt with.",
    # general
    "What are your current savings account interest rates?",
    "How do I add a joint account holder?",
    "Can I change my statement delivery to paperless?",
    "What documents do I need to open a business account?",
    "How long does an international wire transfer take?",
    # escalation
    "I need to speak to a manager immediately about my account.",
    "Someone has stolen my identity and opened accounts in my name.",
    "I am filing a formal complaint with the financial regulator.",
]


def _generate_loan_cases(n: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    cases = []
    for _ in range(n):
        gender = rng.choices(
            ["female", "male", "non-binary"],
            weights=[46, 46, 8],
        )[0]
        age = rng.randint(22, 65)
        income = rng.randint(28_000, 145_000)
        # credit score shaped to produce a mix of outcomes
        credit_score = rng.choices(
            range(550, 825, 5),
            weights=[1 if s < 650 else 2 if s < 720 else 3 for s in range(550, 825, 5)],
        )[0]
        loan_amount = rng.choice([5_000, 10_000, 15_000, 20_000, 25_000, 35_000, 50_000, 75_000])
        cases.append(
            dict(age=age, gender=gender, income=income,
                 credit_score=credit_score, loan_amount=loan_amount)
        )
    return cases


def _generate_support_cases(n: int, seed: int) -> list[str]:
    rng = random.Random(seed + 1)
    return rng.choices(_SUPPORT_POOL, k=n)


def main() -> None:
    parser = argparse.ArgumentParser(description="RAI Client simulator")
    parser.add_argument("--accelerated", action="store_true",
                        help="Run all cases with no delays (default for demo)")
    parser.add_argument("--loans", type=int, default=24,
                        help="Number of loan cases (default 24)")
    parser.add_argument("--support", type=int, default=20,
                        help="Number of support cases (default 20)")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    tracer = trace.get_tracer("rai_client.simulator")
    loan_cases = _generate_loan_cases(args.loans, args.seed)
    support_cases = _generate_support_cases(args.support, args.seed)

    print(f"=== RAI Simulator ({'accelerated' if args.accelerated else 'real-time'}) ===")
    print(f"Loans: {len(loan_cases)}  |  Support: {len(support_cases)}  |  seed={args.seed}\n")

    # ── Loan batch ────────────────────────────────────────────────────────────
    approved = denied = review = 0
    print("── Loan Decisions ──────────────────────────────────────────")
    with tracer.start_as_current_span("sim_loan_batch"):
        for i, case in enumerate(loan_cases, 1):
            t0 = time.time_ns()
            result = run_loan_decision(**case)
            t1 = time.time_ns()
            d = result["decision"]
            if d == "approved": approved += 1
            elif d == "denied":  denied += 1
            else:                review += 1
            write_span_record("loan_decision", {
                "rai.applicant.age":          case["age"],
                "rai.applicant.gender":       case["gender"],
                "rai.applicant.income":       case["income"],
                "rai.applicant.credit_score": case["credit_score"],
                "rai.applicant.loan_amount":  case["loan_amount"],
                "rai.decision":   result["decision"],
                "rai.risk_level": result["risk_level"],
                "rai.risk_score": result["risk_score"],
            }, t0, t1)
            print(
                f"  [{i:02d}] age={case['age']:2d}  gender={case['gender']:10s}  "
                f"credit={case['credit_score']}  → {d:8s}  (risk: {result['risk_level']})"
            )
    print(f"\n  Summary: approved={approved}  denied={denied}  review={review}")

    # ── Support batch ─────────────────────────────────────────────────────────
    flagged_count = 0
    print("\n── Support Chats ───────────────────────────────────────────")
    with tracer.start_as_current_span("sim_support_batch"):
        for i, msg in enumerate(support_cases, 1):
            t0 = time.time_ns()
            result = run_support_chat(msg)
            t1 = time.time_ns()
            write_span_record("support_chat", {
                "rai.intent":           result["intent"],
                "rai.sentiment":        result["sentiment"],
                "rai.toxicity_flagged": result["toxicity_flagged"],
                "rai.safety_category":  result["safety_category"],
            }, t0, t1)
            flag = "⚠ " if result["toxicity_flagged"] else "  "
            if result["toxicity_flagged"]:
                flagged_count += 1
            print(
                f"  [{i:02d}]{flag}intent={result['intent']:11s}  "
                f"sentiment={result['sentiment']:8s}  "
                f"safety={result['safety_category']}"
            )
    print(f"\n  Summary: flagged={flagged_count}/{len(support_cases)}")

    # ── Flush ─────────────────────────────────────────────────────────────────
    tp = trace.get_tracer_provider()
    if hasattr(tp, "force_flush"):
        print("\nFlushing spans to Arize + data/spans.jsonl...", end=" ", flush=True)
        tp.force_flush(timeout_millis=20_000)
        print("done.")

    print("\nSimulation complete.")
    print("→ Now run: python scripts/daily_report.py")


if __name__ == "__main__":
    main()
