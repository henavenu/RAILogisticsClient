#!/usr/bin/env python3
"""
One-shot helper — create PE-owned runtime tables in the shared PAP database.

Run this once if the Policy Engine was started before the DB migration was
complete (pe_app_trust_scores, pe_sessions, pe_session_summaries may be absent).

Usage:
    cd policy-engine
    python ..\tests\init_pe_db_tables.py

Or with explicit path:
    python tests\init_pe_db_tables.py --db "C:/path/to/pap.db"
"""
import argparse
import os
import sys
from pathlib import Path

# Allow running from repo root or policy-engine/
_REPO_ROOT = Path(__file__).parent.parent
_PE_DIR    = _REPO_ROOT / "policy-engine"

# Add policy-engine to sys.path so imports resolve correctly
if str(_PE_DIR) not in sys.path:
    sys.path.insert(0, str(_PE_DIR))

# Load .env files in the same order PE does
from dotenv import load_dotenv
load_dotenv(_PE_DIR / ".env")
load_dotenv(_REPO_ROOT / ".env")

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--db", help="Absolute path to pap.db (overrides AUDIT_SQLITE_PATH)")
args = parser.parse_args()

if args.db:
    os.environ["AUDIT_SQLITE_PATH"] = args.db

db_path = os.getenv("AUDIT_SQLITE_PATH", "").strip()
if not db_path:
    print("ERROR: AUDIT_SQLITE_PATH is not set. Use --db <path> or set the env var.", file=sys.stderr)
    sys.exit(1)

if not Path(db_path).exists():
    print(f"ERROR: DB file not found: {db_path}", file=sys.stderr)
    sys.exit(1)

print(f"Target DB: {db_path}")

try:
    from core.database import init_pe_tables, engine
    from core.pe_models import PeBase  # noqa: F401 – registers models

    init_pe_tables()
    print("PE-owned tables created/verified:")
    print("  pe_sessions")
    print("  pe_session_summaries")
    print("  pe_app_trust_scores")
    print()
    print("Done. Restart the Policy Engine to use the DB-backed session store")
    print("and DbAppTrustTracker.")

except Exception as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    import traceback
    traceback.print_exc()
    sys.exit(1)
