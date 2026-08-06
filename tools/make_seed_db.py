#!/usr/bin/env python3
"""
Generate pap.seed.db — the CONFIG-ONLY snapshot of the PAP database that ships in
the deployment image (see Dockerfile / docker/entrypoint.sh / .dockerignore).

Why this exists
───────────────
The dev pap.db is ~116 MB, almost entirely AUDIT/runtime history
(metric_snapshots, audit_evaluation_*, tier2_eval_scores, pe_*, …). The deployed
container needs the *policy configuration* (every config_* table) but NOT that
audit backlog. This tool copies the source DB and empties every non-config table,
preserving the FULL schema (so PAP boots and the now-empty audit tables fill at
runtime) and the alembic_version (so migrations stay at head). Result: ~600 KB.

What it keeps vs empties
────────────────────────
  KEEP (rows):  config_*               — policy/agent/provider/pricing config
                alembic_version        — migration head marker
                sqlite_sequence        — sqlite internal autoincrement state
  EMPTY (rows): everything else        — audit_*, metric_snapshots, pe_*,
                                         governance_alerts, *_trust_score*, …
  (all table SCHEMAS are preserved in both cases — only rows are deleted)

Usage
─────
  python tools/make_seed_db.py                         # default paths
  python tools/make_seed_db.py --src pap/config/pap.db --out pap/config/pap.seed.db

Run this after any policy change you want reflected in the deployed image, then
rebuild. (For a text/diff-friendly snapshot instead, use export_policy_config.py.)
"""
import argparse
import os
import shutil
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_SRC = _REPO_ROOT / "pap" / "config" / "pap.db"
_DEFAULT_OUT = _REPO_ROOT / "pap" / "config" / "pap.seed.db"

# Rows in these are preserved; everything else is emptied. Schemas are never dropped.
_KEEP_EXACT = {"alembic_version", "sqlite_sequence"}
_KEEP_PREFIXES = ("config_",)


def _keep_rows(table: str) -> bool:
    return table in _KEEP_EXACT or table.startswith(_KEEP_PREFIXES)


def make_seed(src: str, out: str) -> None:
    if not (os.path.exists(src) and os.path.getsize(src) > 0):
        sys.exit(f"ERROR: source DB not found or empty: {src}")

    # Use SQLite's ONLINE BACKUP API for a transactionally-consistent snapshot —
    # safe even if a service (e.g. a running local PAP) is writing to src right now.
    # A plain shutil.copyfile of a live SQLite file can capture a torn page mid-write
    # → "database disk image is malformed" in the consumer. Open src read-only.
    if os.path.exists(out):
        os.remove(out)
    _src = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    con = sqlite3.connect(out)
    with con:
        _src.backup(con)
    _src.close()
    cur = con.cursor()

    tables = [t for (t,) in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    config_tables = [t for t in tables if t.startswith(_KEEP_PREFIXES)]
    if not config_tables:
        sys.exit("ERROR: no config_* tables found — is this the PAP database?")

    emptied = 0
    dropped_rows = 0
    for t in tables:
        if _keep_rows(t):
            continue
        n = cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        if n:
            cur.execute(f'DELETE FROM "{t}"')
            emptied += 1
            dropped_rows += n
    con.commit()
    # Force DELETE (rollback) journal mode on the SEED. The source pap.db runs in
    # WAL mode, and both the backup API and VACUUM preserve that persistent flag —
    # but a WAL-mode seed, once copied to pap.db in the container, makes SQLite use
    # WAL on Docker's overlayfs, where the mmap'd -shm is unsupported → "database
    # disk image is malformed". DELETE mode is overlayfs-safe. Set it BEFORE VACUUM
    # so the rewritten file is persistently DELETE-mode.
    cur.execute("PRAGMA journal_mode=DELETE")
    cur.execute("VACUUM")
    con.commit()

    seed_mode = cur.execute("PRAGMA journal_mode").fetchone()[0]
    kept_rows = sum(
        cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in config_tables
    )
    ver = cur.execute("SELECT version_num FROM alembic_version").fetchone()
    con.close()

    # Update the "applied snapshot" marker that seed_local.py reads. The source
    # pap.db already reflects this snapshot's config (the snapshot was made FROM it),
    # so marking it applied means the dev's NEXT start won't re-seed (audit survives).
    # A colleague's git pull brings the new pap.seed.db but NOT this marker
    # (gitignored), so THEIR seed_local sees a hash mismatch and re-seeds. Exactly right.
    import hashlib
    _h = hashlib.sha256()
    with open(out, "rb") as _f:
        for _chunk in iter(lambda: _f.read(1 << 20), b""):
            _h.update(_chunk)
    (Path(out).parent / ".pap_seed_applied").write_text(_h.hexdigest())

    print(f"OK: {out}")
    print(f"  size           : {os.path.getsize(src)/1e6:.1f} MB  ->  {os.path.getsize(out)/1024:.0f} KB")
    print(f"  config tables  : {len(config_tables)} kept ({kept_rows} rows)")
    print(f"  emptied tables : {emptied} (audit/runtime, {dropped_rows} rows dropped)")
    print(f"  alembic_version: {ver[0] if ver else '(none)'}")
    print(f"  journal_mode   : {seed_mode}")
    print(f"  marker written : {(Path(out).parent / '.pap_seed_applied').name} (seed_local won't re-seed your dev DB)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Make the config-only PAP seed DB.")
    ap.add_argument("--src", default=str(_DEFAULT_SRC), help="Source PAP DB (default: pap/config/pap.db)")
    ap.add_argument("--out", default=str(_DEFAULT_OUT), help="Output seed DB (default: pap/config/pap.seed.db)")
    args = ap.parse_args()
    make_seed(args.src, args.out)


if __name__ == "__main__":
    main()
