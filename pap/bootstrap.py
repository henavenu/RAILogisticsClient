#!/usr/bin/env python3
"""
Bootstrap the PAP database on a fresh clone / new node.

Usage
-----
From the REPO ROOT:   python pap/bootstrap.py
From the PAP dir:     python bootstrap.py          (cd pap first)

What this does
--------------
  1. Loads pap/.env (and root .env as fallback)
  2. Creates the pap/config/ directory if it does not exist
  3. Initialises the database schema via SQLAlchemy create_all
  4. Seeds all config tables from hardcoded Python data (idempotent — safe to re-run)
  5. Seeds model-pricing reference data
  6. Prints a clear summary of what was inserted vs already present

Note: YAML seed files have been removed. All seed data is now managed entirely
through the PAP database via pap/core/seed.py.

Safe to re-run — all seeders skip existing rows and never overwrite operator changes.
"""

from __future__ import annotations

import sys
import os
import logging
from pathlib import Path

# ── Path wiring ────────────────────────────────────────────────────────────────
# bootstrap.py lives in pap/, so __file__ is always the reliable anchor.
_PAP_DIR = Path(__file__).resolve().parent   # …/rai-trust-platform/pap/

# Add pap/ to sys.path so "from core.xxx import …" works when this script is
# invoked from the repo root (python pap/bootstrap.py).
if str(_PAP_DIR) not in sys.path:
    sys.path.insert(0, str(_PAP_DIR))

# ── Environment ────────────────────────────────────────────────────────────────
from dotenv import load_dotenv           # noqa: E402 (after path setup)

load_dotenv(_PAP_DIR / ".env")           # service-level (wins)
load_dotenv(_PAP_DIR.parent / ".env")   # root fallback

# ── Logging ────────────────────────────────────────────────────────────────────
# Use stdout (not stderr) so log messages and print() output stay in order.
logging.basicConfig(
    level=logging.WARNING,               # suppress SQLAlchemy noise
    format="   [!!] %(name)s -- %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("bootstrap")


# ── Helpers ────────────────────────────────────────────────────────────────────

_GREEN  = "\033[32m"
_YELLOW = "\033[33m"
_RED    = "\033[31m"
_BOLD   = "\033[1m"
_RESET  = "\033[0m"

# Use only ASCII printable chars so the script works on Windows cp1252 terminals
# as well as UTF-8 Linux/Mac terminals.
def _ok(msg: str)   -> None: print(f"   {_GREEN}[OK]{_RESET}  {msg}")
def _warn(msg: str) -> None: print(f"   {_YELLOW}[!!]{_RESET}  {msg}")
def _err(msg: str)  -> None: print(f"   {_RED}[XX]{_RESET}  {msg}")
def _h(msg: str)    -> None: print(f"\n{_BOLD}>>  {msg}{_RESET}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    # Print the banner first, then import (imports may emit log warnings).
    print()
    print(f"{_BOLD}{'-' * 62}{_RESET}")
    print(f"{_BOLD}  RAI Trust Platform -- PAP Bootstrap{_RESET}")
    print(f"{'-' * 62}")
    print(f"  PAP dir  : {_PAP_DIR}")

    # Import after banner so DATABASE_URL (resolved in database.py) is ready.
    from core.database    import init_db, SessionLocal, DATABASE_URL  # noqa: E402
    from core.seed        import seed_all                              # noqa: E402
    from core.seed_pricing import seed_pricing                        # noqa: E402

    print(f"  Database : {DATABASE_URL}")
    print()

    # ── 1. Schema ─────────────────────────────────────────────────────────────
    _h("Initialising database schema …")
    try:
        init_db()
        _ok("Schema ready (create_all complete)")
    except Exception as exc:
        _err(f"Schema init failed: {exc}")
        return 1

    # ── 2. Seed config tables ─────────────────────────────────────────────────
    _h("Seeding config tables …")
    try:
        with SessionLocal() as db:
            results = seed_all(db)
            total   = sum(results.values())

            if total == 0:
                _ok("All tables already populated — nothing to insert")
            else:
                for table, count in results.items():
                    if count:
                        _ok(f"{table:<44} +{count} rows")
                    else:
                        print(f"       {table:<44} (already populated)")

            # ── 3. Pricing ─────────────────────────────────────────────────
            _h("Seeding model-pricing reference data …")
            pricing_count = seed_pricing(db)
            if pricing_count:
                _ok(f"config_model_pricing                       +{pricing_count} rows")
            else:
                _ok("Model pricing already populated")

    except Exception as exc:
        _err(f"Seeding failed: {exc}")
        logger.exception("Seed error")
        return 1

    # ── Done ──────────────────────────────────────────────────────────────────
    print()
    print(f"{_BOLD}{'-' * 62}{_RESET}")
    print(f"{_BOLD}  Bootstrap complete.{_RESET}")
    print()
    print("  Start services:")
    print(f"    PAP (port 3002)  --  cd pap && uvicorn main:app --port 3002")
    print( "    or use the Makefile at the repo root: make pap")
    print(f"{'-' * 62}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
