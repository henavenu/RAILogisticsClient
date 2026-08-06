#!/usr/bin/env python3
"""
Initialise the PAP database (pap.db) from the committed snapshot (pap.seed.db).

THIS IS THE SINGLE SOURCE OF DB INITIALISATION. start_all.bat (local) and
docker/entrypoint.sh (container) both call it, so a fresh `git clone`, a `git pull`,
and a container start are IDENTICAL. The committed snapshot is the source of truth
for BOTH schema and config — we never rely on init_db()/alembic to (re)build the
schema, which has drifted (missing content_snippet / audit_interaction_events) and
caused the "table ... has no column" errors on fresh pulls.

Logic (idempotent, safe to run on every start):
  • If pap.db is MISSING, or the snapshot CHANGED since it was last applied
    (tracked by the hash marker .pap_seed_applied), then (re)materialise pap.db
    from pap.seed.db and remove stale WAL/SHM sidecars.
  • Otherwise leave pap.db untouched — your audit data + edits survive restarts.

clone   → no pap.db        → seed (marker written)
pull    → snapshot hash ≠ marker → RE-seed (your stale DB is replaced; marker updated)
restart → snapshot hash = marker → skip (pap.db preserved)
dev snapshot → make_seed_db.py writes the marker too, so your next start does NOT
              re-seed (your dev pap.db already reflects the snapshot).

Take a NEW snapshot manually with:  python tools/make_seed_db.py   (or snapshot.bat)
"""
import hashlib
import os
import shutil
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
SEED = _ROOT / "pap" / "config" / "pap.seed.db"
# Honour AUDIT_SQLITE_PATH if the caller set it (entrypoint does); else the default.
DB = Path(os.getenv("AUDIT_SQLITE_PATH", str(_ROOT / "pap" / "config" / "pap.db")))
MARKER = DB.parent / ".pap_seed_applied"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    if not (SEED.exists() and SEED.stat().st_size > 0):
        print(f"[seed_local] ERROR: snapshot missing/empty: {SEED}", file=sys.stderr)
        return 1

    seed_hash = _sha256(SEED)
    applied = MARKER.read_text().strip() if MARKER.exists() else ""

    if DB.exists() and applied == seed_hash:
        print(f"[seed_local] pap.db already matches the current snapshot — preserved "
              f"({seed_hash[:12]}…).")
        return 0

    # (Re)materialise from the snapshot. Remove stale sidecars first — a mismatched
    # WAL applied to the fresh copy reads as "database disk image is malformed".
    DB.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("-wal", "-shm", "-journal"):
        side = Path(str(DB) + suffix)
        try:
            side.unlink()
        except FileNotFoundError:
            pass
    shutil.copyfile(SEED, DB)
    MARKER.write_text(seed_hash)

    reason = "created" if not applied else "snapshot changed → re-seeded"
    print(f"[seed_local] pap.db {reason} from pap.seed.db ({seed_hash[:12]}…). "
          f"Schema + config from snapshot; audit starts empty.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
