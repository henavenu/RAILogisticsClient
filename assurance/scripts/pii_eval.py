"""
PII eval — scans Arize spans over a configurable window for personally identifiable information.

Usage:
    python scripts/pii_eval.py
    python scripts/pii_eval.py --hours 4
    python scripts/pii_eval.py --hours 1 --project rai-assurance
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
    parser = argparse.ArgumentParser(description="PII eval over an Arize span window")
    parser.add_argument("--hours",   type=int, default=1,              help="Window size in hours (default: 1)")
    parser.add_argument("--project", default="rai-assurance",          help="Arize project name")
    args = parser.parse_args()

    os.environ.setdefault("ASSURANCE_PROVIDER", "arize")

    from evals.evals import run_pii_eval_window

    print(f"\n── PII Eval ────────────────────────────────────────────────")
    print(f"  Project : {args.project}")
    print(f"  Window  : last {args.hours}h")
    print()

    report = run_pii_eval_window(since_hours=args.hours, project=args.project)

    total   = report["total_spans"]
    flagged = report["flagged_spans"]
    rate    = report["pii_rate"]
    status  = "WARNING" if flagged > 0 else "OK"

    print(f"  Total spans   : {total}")
    print(f"  Flagged spans : {flagged}")
    print(f"  PII rate      : {rate:.1%}  [{status}]")

    if report["findings"]:
        print(f"\n── Findings ────────────────────────────────────────────────")
        for f in report["findings"]:
            types = ", ".join(f["pii_types"])
            print(f"\n  Span {f['span_index'] + 1}  use_case={f['use_case']}  pii=[{types}]")
            for field, ftypes in f["fields"].items():
                print(f"    └─ {field}: {', '.join(ftypes)}")
    else:
        print("\n  No PII detected in any span.")

    print()


if __name__ == "__main__":
    main()
