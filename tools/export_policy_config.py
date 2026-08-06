#!/usr/bin/env python3
"""
Export the RAI Trust Platform POLICY CONFIGURATION (DDL + DML) to a portable .sql
file so it can be transferred between environments (laptop → Docker → cloud).

Why this exists
───────────────
For the Policy Engine + PAP, the *database* is the source of truth — the YAML
files (policies.yaml, policy_packs.yaml, …) are vestigial seed-only and are NOT
read at runtime. (Sentinel & Coordinator keep their own policy.yaml — those are
NOT touched by this tool.) There is no built-in way to move DB-held policy config
between environments, which is why settings like a pack's hitl_trigger "don't
transfer" to Docker. This tool closes that gap.

What it exports
───────────────
Every `config_*` table (packs, pack→dimension assignments, policies, dimensions,
providers, threshold profiles, apps, service configs, session policy, pricing, …)
as DROP TABLE + CREATE TABLE (DDL) + INSERT (DML). Runtime/audit tables
(audit_*, governance_alerts, metric_snapshots, pe_*, …) are deliberately excluded.

Usage
─────
  python tools/export_policy_config.py                      # default DB + default out file
  python tools/export_policy_config.py --db path/to.db --out config.sql

Then in the target environment:
  python tools/import_policy_config.py --in config.sql      # loads into that env's DB
  (restart the Policy Engine afterwards — it loads pack/zero-tolerance config at startup)
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def resolve_db(arg_db: str | None) -> str:
    """Resolve the PAP sqlite DB path: --db wins, else PAP_DATABASE_URL, else default."""
    if arg_db:
        return arg_db
    url = os.getenv("PAP_DATABASE_URL", "").strip()
    if url.startswith("sqlite:///"):
        p = Path(url[len("sqlite:///"):])
        if not p.is_absolute():
            p = (_REPO_ROOT / "pap" / p).resolve()
        return str(p)
    return str(_REPO_ROOT / "pap" / "config" / "pap.db")


def sql_literal(v) -> str:
    """Render a Python value as a SQLite SQL literal (DML-safe)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, bytes):
        return "X'" + v.hex() + "'"
    return "'" + str(v).replace("'", "''") + "'"


def export(db_path: str, out_path: str) -> None:
    if not (os.path.exists(db_path) and os.path.getsize(db_path) > 0):
        sys.exit(f"ERROR: database not found or empty: {db_path}")

    con = sqlite3.connect(db_path)
    cur = con.cursor()

    config_tables = [
        name for (name,) in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'config_%' ORDER BY name"
        )
    ]
    if not config_tables:
        sys.exit("ERROR: no config_* tables found — is this the PAP database?")

    lines: list[str] = []
    lines.append(f"-- RAI Trust Platform — policy config export")
    lines.append(f"-- source DB : {db_path}")
    lines.append(f"-- exported  : {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"-- tables    : {len(config_tables)}")
    lines.append("PRAGMA foreign_keys=OFF;")
    lines.append("BEGIN TRANSACTION;")

    total_rows = 0
    for t in config_tables:
        ddl = cur.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone()[0]
        cols = [r[1] for r in cur.execute(f'PRAGMA table_info("{t}")')]
        collist = ", ".join(f'"{c}"' for c in cols)
        rows = cur.execute(f'SELECT * FROM "{t}"').fetchall()

        lines.append("")
        lines.append(f"-- ── {t}  ({len(rows)} rows) ─────────────────────────────")
        lines.append(f'DROP TABLE IF EXISTS "{t}";')
        lines.append(ddl.strip().rstrip(";") + ";")
        for row in rows:
            vals = ", ".join(sql_literal(v) for v in row)
            lines.append(f'INSERT INTO "{t}" ({collist}) VALUES ({vals});')
        total_rows += len(rows)

        # Also export any indexes on this table
        for (idx_sql,) in cur.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? "
            "AND sql IS NOT NULL", (t,)
        ):
            lines.append(idx_sql.strip().rstrip(";") + ";")

    lines.append("")
    lines.append("COMMIT;")
    lines.append("PRAGMA foreign_keys=ON;")

    Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    con.close()
    print(f"OK: exported {len(config_tables)} config tables, {total_rows} rows -> {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Export PAP policy config (DDL+DML).")
    ap.add_argument("--db", default=None, help="Path to the PAP sqlite DB (default: PAP_DATABASE_URL or pap/config/pap.db)")
    ap.add_argument("--out", default=str(_REPO_ROOT / "policy_config_export.sql"),
                    help="Output .sql file (default: ./policy_config_export.sql)")
    args = ap.parse_args()
    export(resolve_db(args.db), args.out)


if __name__ == "__main__":
    main()
