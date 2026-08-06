"""
Sentinel & Coordinator — Automated Test Runner
===============================================
Zero-interaction wrapper around run_sentinel_scenarios.py.

Usage (from repo root, services already running):
  python tests/_run_agent_tests.py

What it does:
  1. Health-checks all 4 required services (PE, Sentinel, Coordinator, PAP)
  2. Runs all 7 sentinel scenarios with --no-fail-fast
  3. Saves full output to tests/logs/agent_test_<timestamp>.log
  4. Prints a concise pass/fail summary table

No interactive prompts — safe to run as a single background command.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# Force UTF-8 stdout/stderr — Windows cp1252 cannot encode box-drawing chars
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Config ────────────────────────────────────────────────────────────────────

PE_URL          = os.getenv("PE_URL",          "http://localhost:3000")
SENTINEL_URL    = os.getenv("SENTINEL_URL",    "http://localhost:3012")
COORDINATOR_URL = os.getenv("COORDINATOR_URL", "http://localhost:3008")
PAP_URL         = os.getenv("PAP_URL",         "http://localhost:3002")

SERVICES = [
    ("Policy Engine",  PE_URL,          "/health"),
    ("Sentinel",       SENTINEL_URL,    "/health"),
    ("Coordinator",    COORDINATOR_URL, "/health"),
    ("PAP",            PAP_URL,         "/docs"),   # PAP has no /health — /docs always 200
]

REPO_ROOT   = Path(__file__).parent.parent
SCENARIO_PY = REPO_ROOT / "tests" / "run_sentinel_scenarios.py"
LOG_DIR     = REPO_ROOT / "tests" / "logs"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _sep(char="─", width=70) -> str:
    return char * width

def _check_service(name: str, base: str, path: str) -> tuple[bool, str]:
    """
    Any HTTP response (even 404/405) means the service process is running.
    Only connection refused or timeout means it is truly down.
    """
    url = base.rstrip("/") + path
    try:
        r = requests.get(url, timeout=5, allow_redirects=True)
        return True, f"HTTP {r.status_code}"
    except requests.exceptions.ConnectionError:
        return False, "connection refused"
    except requests.exceptions.Timeout:
        return False, "timeout (5s)"
    except Exception as e:
        return False, str(e)

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts_file   = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path  = LOG_DIR / f"agent_test_{ts_file}.log"

    print(_sep("═"))
    print(f"  RAI Trust Platform — Sentinel & Coordinator Test Run")
    print(f"  Started : {_ts()}")
    print(f"  Log     : {log_path.relative_to(REPO_ROOT)}")
    print(_sep("═"))

    # ── 1. Health checks ──────────────────────────────────────────────────────
    print("\n[1/2] Service health checks\n")
    all_up = True
    for name, base, path in SERVICES:
        ok, detail = _check_service(name, base, path)
        status = "  OK " if ok else " FAIL"
        icon   = "✓" if ok else "✗"
        print(f"  {icon} {name:<22} {base:<32} {detail}")
        if not ok:
            all_up = False

    if not all_up:
        print(f"\n  {_sep()}")
        print("  ✗  One or more services are not reachable.")
        print("     Start all services before running the test suite:")
        print("       make all   (or start PE / Sentinel / Coordinator / PAP individually)")
        print(f"  {_sep()}\n")
        return 1

    print(f"\n  All services reachable — proceeding.\n")

    # ── 2. Run scenario suite ─────────────────────────────────────────────────
    print(f"[2/2] Running sentinel scenario suite (S01–S07)\n")
    print(f"  This runs all 7 pre-check scenarios end-to-end.")
    print(f"  Wait times: Sentinel {os.getenv('SENTINEL_WAIT_S','15')}s (Phase A+B) + "
          f"Coordinator {os.getenv('COORD_WAIT_S','10')}s per scenario.")
    print(f"  Estimated wall-clock time: ~5–8 minutes\n")
    print(_sep())

    cmd = [
        sys.executable, str(SCENARIO_PY),
        "--no-fail-fast",
    ]
    env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        # Phase B (LLM enrichment) can take 5-15s — set conservative defaults
        # so the test waits long enough to read the LLM-enriched verdict.
        "SENTINEL_WAIT_S": os.getenv("SENTINEL_WAIT_S", "15"),
        "COORD_WAIT_S":    os.getenv("COORD_WAIT_S",    "10"),
    }

    start = time.monotonic()
    lines: list[str] = []

    # Stream to stdout AND buffer for log file
    with open(log_path, "w", encoding="utf-8") as log_fh:
        log_fh.write(f"RAI Trust Platform — Agent Test Run\n")
        log_fh.write(f"Started : {_ts()}\n")
        log_fh.write(f"Command : {' '.join(cmd)}\n")
        log_fh.write(_sep() + "\n\n")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            encoding="utf-8",
            errors="replace",
        )
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            log_fh.write(line)
            lines.append(line)

        proc.wait()

    elapsed = time.monotonic() - start
    rc      = proc.returncode

    # ── 3. Summary ────────────────────────────────────────────────────────────
    print(_sep("═"))
    print(f"\n  Test run complete in {elapsed:.1f}s")
    print(f"  Exit code : {rc}  ({'PASSED' if rc == 0 else 'FAILURES DETECTED'})")
    print(f"  Full log  : {log_path}\n")

    # Parse scenario results from output
    results: list[tuple[str, str, str]] = []   # (id, name, result)
    for line in lines:
        l = line.strip()
        if l.startswith("S0") and ("PASS" in l or "FAIL" in l or "SKIP" in l):
            parts = l.split()
            sid   = parts[0] if parts else "?"
            res   = "PASS" if "PASS" in l else "FAIL" if "FAIL" in l else "SKIP"
            name  = " ".join(p for p in parts[1:] if p not in ("PASS","FAIL","SKIP","OK","✓","✗"))
            results.append((sid, name.strip(" —:"), res))

    if results:
        print("  Scenario summary:")
        print(f"  {'ID':<6} {'Name':<32} {'Result'}")
        print(f"  {_sep('-', 54)}")
        for sid, name, res in results:
            icon = "✓" if res == "PASS" else "✗" if res == "FAIL" else "·"
            print(f"  {icon} {sid:<5} {name:<32} {res}")
        print()

    print(_sep("═"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
