"""
RAI Assurance daily health report — calls the live API and prints a formatted summary.

Requires uvicorn running: uvicorn api.main:app --reload

Usage:
    python scripts/daily_report.py
    python scripts/daily_report.py --base-url http://localhost:8000
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

BASE_URL = "http://localhost:8000"

_STATUS_ICON  = {"ok": "✓", "warning": "⚠", "critical": "✗"}
_PILLAR_LABEL = {
    "fairness":    "Fairness",
    "safety":      "Safety",
    "reliability": "Reliability",
    "privacy":     "Privacy",
}


def _get(client: httpx.Client, path: str) -> dict:
    resp = client.get(f"{BASE_URL}{path}")
    resp.raise_for_status()
    return resp.json()


def _post(client: httpx.Client, path: str) -> dict:
    resp = client.post(f"{BASE_URL}{path}")
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    global BASE_URL
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=BASE_URL)
    args = parser.parse_args()
    BASE_URL = args.base_url

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"╔══════════════════════════════════════════════════════╗")
    print(f"║        RAI ASSURANCE DAILY REPORT  {now}  ║")
    print(f"╚══════════════════════════════════════════════════════╝\n")

    with httpx.Client(timeout=30) as client:

        print("Recomputing metrics...", end=" ", flush=True)
        try:
            run = _post(client, "/jobs/run")
            print(f"done.  ({run['metrics_computed']} metrics, {run['warnings']} warnings)\n")
        except httpx.ConnectError:
            print("\nERROR: Cannot reach API server.")
            print("Start it with: uvicorn api.main:app --reload")
            sys.exit(1)

        summary = _get(client, "/metrics/summary")
        overall = summary["overall_status"]
        icon = _STATUS_ICON.get(overall, "?")
        print(f"Overall RAI Health: {icon} {overall.upper()}")
        print(f"Total warnings: {summary['total_warnings']} / {summary['total_metrics']}\n")

        all_metrics_resp = _get(client, "/metrics")
        by_name = {m["metric_name"]: m for m in all_metrics_resp.get("metrics", [])}

        for pillar in ["fairness", "safety", "reliability", "privacy"]:
            pdata   = summary["pillars"].get(pillar, {})
            pstatus = pdata.get("status", "ok")
            picon   = _STATUS_ICON.get(pstatus, "?")
            label   = _PILLAR_LABEL.get(pillar, pillar.title())
            print(f"── {label} {picon} {'─' * (44 - len(label))}")

            for name in pdata.get("metrics", []):
                m = by_name.get(name)
                if not m:
                    print(f"   ? {name}  (no data)")
                    continue
                micon     = _STATUS_ICON.get(m["status"], "?")
                val       = m["value"]
                unit      = m.get("unit", "")
                threshold = m.get("threshold")
                thr_str   = f"  threshold={threshold}" if threshold is not None else ""
                print(f"   {micon} {name}")
                print(f"       value={val} {unit}{thr_str}  spans={m['span_count']}")
                if m.get("breakdown"):
                    bd = "  |  ".join(f"{k}: {v}" for k, v in m["breakdown"].items())
                    print(f"       {bd}")
            print()

    warnings = summary["total_warnings"]
    if warnings == 0:
        print("All metrics within thresholds. No action required.")
    else:
        print(f"{warnings} metric(s) exceeded threshold — review ⚠ items above.")


if __name__ == "__main__":
    main()
