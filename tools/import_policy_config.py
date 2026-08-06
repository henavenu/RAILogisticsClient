#!/usr/bin/env python3
"""
Import a policy-config .sql export (produced by export_policy_config.py) into a
target environment's PAP database. This is how DB-held policy config (packs,
hitl_trigger flags, dimension→pack assignments, providers, thresholds, …) is
transferred between environments — e.g. laptop → Docker → cloud.

The export contains DROP TABLE + CREATE TABLE + INSERT for every config_* table,
so importing fully REPLACES the config tables in the target DB. Runtime/audit
tables are never touched.

Usage
─────
  python tools/import_policy_config.py --in policy_config_export.sql
  python tools/import_policy_config.py --in config.sql --db path/to/target.db

After import, RESTART the Policy Engine (it loads pack + zero_tolerance config at
startup). A --dry-run flag prints what would be applied without writing.
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def resolve_db(arg_db: str | None) -> str:
    if arg_db:
        return arg_db
    url = os.getenv("PAP_DATABASE_URL", "").strip()
    if url.startswith("sqlite:///"):
        p = Path(url[len("sqlite:///"):])
        if not p.is_absolute():
            p = (_REPO_ROOT / "pap" / p).resolve()
        return str(p)
    return str(_REPO_ROOT / "pap" / "config" / "pap.db")


def main() -> None:
    ap = argparse.ArgumentParser(description="Import PAP policy config (DDL+DML).")
    ap.add_argument("--in", dest="infile", required=True, help="The .sql export to apply")
    ap.add_argument("--db", default=None, help="Target PAP sqlite DB (default: PAP_DATABASE_URL or pap/config/pap.db)")
    ap.add_argument("--dry-run", action="store_true", help="Validate + summarise without writing")
    args = ap.parse_args()

    infile = Path(args.infile)
    if not infile.exists():
        sys.exit(f"ERROR: export file not found: {infile}")
    script = infile.read_text(encoding="utf-8")

    tables = sorted({
        ln.split('"')[1]
        for ln in script.splitlines()
        if ln.strip().startswith("DROP TABLE")
    })
    inserts = sum(1 for ln in script.splitlines() if ln.strip().startswith("INSERT INTO"))
    print(f"Export contains {len(tables)} config tables, {inserts} INSERT rows.")
    print("Tables:", ", ".join(tables))

    if args.dry_run:
        print("[dry-run] not applied.")
        return

    db_path = resolve_db(args.db)
    # Allow creating a fresh target DB, but warn if it's missing config to merge into.
    con = sqlite3.connect(db_path)
    try:
        con.executescript(script)
        con.commit()
    except Exception as exc:
        con.rollback()
        con.close()
        sys.exit(f"ERROR applying import (rolled back): {exc}")

    print(f"OK: imported policy config into {db_path}")
    # quick verify (print failures here must NOT affect the committed import)
    try:
        packs = con.execute(
            "SELECT pack_id, hitl_trigger, active FROM config_policy_packs ORDER BY pack_id"
        ).fetchall()
        print("config_policy_packs now:")
        for p in packs:
            print(f"   {p[0]:22} hitl_trigger={p[1]} active={p[2]}")
    except Exception:
        pass
    con.close()
    print("\nNEXT: restart the Policy Engine so it reloads pack/zero_tolerance config.")


if __name__ == "__main__":
    main()
