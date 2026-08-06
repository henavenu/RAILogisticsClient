#!/usr/bin/env python3
"""
One-shot helper -- seed config_providers table from the built-in definitions
in pap/core/seed.py.

Safe to re-run (idempotent -- skips existing rows).

Usage:
    cd rai-trust-platform
    python tests/seed_providers.py
"""
import os
import sys
from pathlib import Path

_ROOT  = Path(__file__).resolve().parent.parent
_PAP   = _ROOT / "pap"
if str(_PAP) not in sys.path:
    sys.path.insert(0, str(_PAP))

# Set DB URL to absolute path BEFORE importing core.database so the engine
# is created against the correct file regardless of cwd.
_DB_PATH = _PAP / "config" / "pap.db"
os.environ.setdefault("PAP_DATABASE_URL", f"sqlite:///{_DB_PATH}")

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

from core.database import SessionLocal
from core.seed import seed_providers

db = SessionLocal()
try:
    n = seed_providers(db)
    if n:
        print(f"Seeded {n} provider(s) into config_providers.")
    else:
        print("All providers already present — nothing to insert.")
    print("Done.")
finally:
    db.close()
