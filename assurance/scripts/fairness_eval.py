"""
Fairness eval — Phoenix LLM-as-judge over Arize spans.

Two metrics:
  - demographic_bias_rate     (support_chat: biased treatment in agent responses)
  - decision_fairness_rate    (loan_decision: rationale based on protected attributes)

Usage:
    python scripts/fairness_eval.py
    python scripts/fairness_eval.py --hours 4
    python scripts/fairness_eval.py --hours 1 --project rai-assurance
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    parser = argparse.ArgumentParser(description="Fairness eval over an Arize span window")
    parser.add_argument("--hours",   type=int, default=1,              help="Window size in hours (default: 1)")
    parser.add_argument("--project", default="rai-assurance",          help="Arize project name")
    args = parser.parse_args()

    os.environ.setdefault("ASSURANCE_PROVIDER", "arize")

    from evals.evals import run_fairness_eval_window

    print(f"\n── Fairness Eval (Phoenix LLM-as-judge) ────────────────────")
    print(f"  Project : {args.project}")
    print(f"  Window  : last {args.hours}h")
    print(f"  Judge   : {os.getenv('JUDGE_MODEL', 'claude-haiku-4-5')}")
    print()

    report = run_fairness_eval_window(since_hours=args.hours, project=args.project)

    bias = report["demographic_bias"]
    deci = report["decision_fairness"]

    print(f"  ── demographic_bias_rate (support_chat) ──")
    print(f"    spans judged    : {report['support_judged']}")
    print(f"    biased          : {bias.breakdown.get('biased', 0)}")
    print(f"    rate            : {bias.value:.1%}  [{bias.status.upper()}]")

    print()
    print(f"  ── decision_fairness_rate (loan_decision) ──")
    print(f"    spans judged    : {report['loan_judged']}")
    print(f"    discriminatory  : {deci.breakdown.get('discriminatory', 0)}")
    print(f"    rate            : {deci.value:.1%}  [{deci.status.upper()}]")

    if report["findings"]:
        print(f"\n── Findings ────────────────────────────────────────────────")
        for f in report["findings"]:
            print(f"\n  use_case={f['use_case']}  label={f['label']}")
            for k, v in f["input"].items():
                snippet = str(v)
                if len(snippet) > 120:
                    snippet = snippet[:117] + "..."
                print(f"    └─ {k}: {snippet}")
    else:
        print("\n  No fairness violations detected.")

    print()


if __name__ == "__main__":
    main()
