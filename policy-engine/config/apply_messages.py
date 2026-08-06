"""
apply_messages.py — Write the reviewed MessageWriter fields to the live database.

Prerequisites
─────────────
  1. Migration 0017 applied:
       cd pap && alembic upgrade 0017

  2. policies.yaml reviewed and approved by the policy team.

Usage
─────
  python policy-engine/config/apply_messages.py [--dry-run] [--db PATH]

  --dry-run   Print SQL UPDATE statements without executing them.
  --db PATH   Override DB path (default: pap/config/pap.db).

What it does
────────────
  For every dimension in policy_messages.yaml, runs one SQL UPDATE on config_policies
  to set: policy_code, normative_action, normative_guidance,
          remediation_normative (if provided), remediation_coercive (if provided).

  Dimensions that do not yet have a row in config_policies are flagged as
  MISSING — the operator must create the policy in PAP first.

  Dimensions with no change (all fields already match) are skipped with SKIP.

  After writing, calls POST /internal/reload on the Policy Engine so the
  new fields take effect immediately without a restart.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import textwrap
from pathlib import Path
from typing import Optional

# ── Inline YAML loader (avoids PyYAML dep in the PE venv) ─────────────────────
try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False


def _load_yaml(path: Path) -> dict:
    if not _YAML_AVAILABLE:
        # Minimal fallback: try ruamel or plain string heuristics
        try:
            from ruamel.yaml import YAML
            ry = YAML()
            with open(path, encoding="utf-8") as f:
                return ry.load(f)
        except ImportError:
            print("ERROR: PyYAML or ruamel.yaml is required. Install with: pip install pyyaml")
            sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── DB helpers ─────────────────────────────────────────────────────────────────

_MIGRATION_COLS = ("policy_code", "normative_action", "normative_guidance")

def _check_migration(conn: sqlite3.Connection) -> bool:
    """Verify migration 0017 has been applied (columns exist)."""
    cur = conn.execute("PRAGMA table_info(config_policies)")
    cols = {row[1] for row in cur.fetchall()}
    missing = [c for c in _MIGRATION_COLS if c not in cols]
    if missing:
        print(f"\nERROR: The following columns are missing from config_policies:")
        for c in missing:
            print(f"  - {c}")
        print("\nRun:  cd pap && alembic upgrade 0017")
        return False
    return True


def _fetch_existing(conn: sqlite3.Connection) -> dict[str, dict]:
    """Return all active global policies keyed by dimension."""
    cur = conn.execute(
        "SELECT dimension, policy_code, normative_action, normative_guidance, "
        "       remediation_normative, remediation_coercive "
        "FROM config_policies WHERE active = 1 AND app_id IS NULL"
    )
    return {
        row[0]: {
            "policy_code":         row[1],
            "normative_action":    row[2],
            "normative_guidance":  row[3],
            "remediation_normative": row[4],
            "remediation_coercive":  row[5],
        }
        for row in cur.fetchall()
    }


# ── Core apply logic ───────────────────────────────────────────────────────────

def apply(
    db_path:  Path,
    yaml_path: Path,
    dry_run:  bool = False,
) -> None:
    data = _load_yaml(yaml_path)
    policies = data.get("policies", [])
    if not policies:
        print("No policies found in YAML.")
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    if not _check_migration(conn):
        conn.close()
        sys.exit(1)

    existing = _fetch_existing(conn)

    counts = {"updated": 0, "skipped": 0, "missing": 0, "errors": 0}

    prefix = "DRY RUN -- " if dry_run else ""
    print(f"\n{prefix}Applying messages from {yaml_path.name} -> {db_path}\n")
    print(f"{'Dimension':<35}  {'Code':<10}  {'Action':<10}  {'Result'}")
    print("-" * 80)

    for entry in policies:
        dim   = entry.get("dimension", "").strip()
        if not dim:
            continue

        code     = entry.get("policy_code")
        action   = entry.get("normative_action", "REVISION")
        guidance = _unwrap(entry.get("normative_guidance"))
        rem_norm = _unwrap(entry.get("remediation_normative"))
        rem_coer = _unwrap(entry.get("remediation_coercive"))

        if dim not in existing:
            print(f"  {dim:<35}  {code or '':<10}  {action:<10}  MISSING — no policy row")
            counts["missing"] += 1
            continue

        cur_row = existing[dim]

        # Build update fields (only what has changed)
        updates: dict[str, Optional[str]] = {}
        if cur_row["policy_code"]        != code:     updates["policy_code"]        = code
        if cur_row["normative_action"]   != action:   updates["normative_action"]   = action
        if cur_row["normative_guidance"] != guidance: updates["normative_guidance"]  = guidance
        if rem_norm and cur_row["remediation_normative"] != rem_norm:
            updates["remediation_normative"] = rem_norm
        if rem_coer and cur_row["remediation_coercive"] != rem_coer:
            updates["remediation_coercive"] = rem_coer

        if not updates:
            print(f"  {dim:<35}  {code or '':<10}  {action:<10}  SKIP (no changes)")
            counts["skipped"] += 1
            continue

        sql = (
            "UPDATE config_policies SET "
            + ", ".join(f"{k} = ?" for k in updates)
            + " WHERE dimension = ? AND app_id IS NULL"
        )
        params = list(updates.values()) + [dim]

        if dry_run:
            print(f"  {dim:<35}  {code or '':<10}  {action:<10}  WOULD UPDATE {list(updates.keys())}")
            _print_sql(sql, params)
        else:
            try:
                conn.execute(sql, params)
                print(f"  {dim:<35}  {code or '':<10}  {action:<10}  UPDATED {list(updates.keys())}")
                counts["updated"] += 1
            except Exception as exc:
                print(f"  {dim:<35}  {'':10}  {'':10}  ERROR: {exc}")
                counts["errors"] += 1

    if not dry_run and counts["updated"] > 0:
        conn.commit()
        print(f"\n[OK] Committed {counts['updated']} update(s) to {db_path.name}")
    elif dry_run:
        print(f"\n(dry-run: no changes written)")

    conn.close()

    print(f"\nSummary: updated={counts['updated']}  skipped={counts['skipped']}  "
          f"missing={counts['missing']}  errors={counts['errors']}")

    if counts["missing"]:
        print(
            "\nNOTE: Dimensions marked MISSING have no policy row yet.\n"
            "Create them in PAP (Policy tab) before re-running this script."
        )

    if counts["updated"] > 0 and not dry_run:
        _reload_pe()


# ── PE hot-reload ──────────────────────────────────────────────────────────────

def _reload_pe() -> None:
    import os
    pe_url = os.getenv("POLICY_ENGINE_URL", "http://localhost:3003")  # PE default port is 3003 (was stale :3000)
    try:
        import httpx
        r = httpx.post(f"{pe_url}/internal/reload", timeout=5.0)
        if r.is_success:
            print(f"\n[OK] Policy Engine reloaded ({r.status_code})")
        else:
            print(f"\n[WARN] Policy Engine reload returned {r.status_code}: {r.text[:200]}")
    except Exception as exc:
        print(f"\n[WARN] Could not reach Policy Engine at {pe_url}: {exc}")
        print("  Restart the PE manually or POST /internal/reload to pick up the changes.")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _unwrap(text: Optional[str]) -> Optional[str]:
    """Collapse YAML block scalars (multi-line strings) to a single line."""
    if not text:
        return None
    return " ".join(str(text).split())


def _print_sql(sql: str, params: list) -> None:
    """Pretty-print a parameterised SQL statement for dry-run output."""
    filled = sql
    for p in params:
        val = f"'{p}'" if isinstance(p, str) else ("NULL" if p is None else str(p))
        filled = filled.replace("?", val, 1)
    print("    SQL: " + textwrap.shorten(filled, 120, placeholder=" ..."))


# ── CLI entry point ────────────────────────────────────────────────────────────

def _default_db() -> Path:
    """Find the live pap.db relative to this script's location."""
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent.parent / "pap" / "config" / "pap.db",
        here.parent.parent / "pap" / "pap.db",
    ]
    for p in candidates:
        if p.exists() and p.stat().st_size > 0:
            return p
    return candidates[0]   # will fail with a clear error if not found


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply MessageWriter field updates to the PAP config_policies table."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would change without writing to the database.",
    )
    parser.add_argument(
        "--db", type=Path, default=None,
        help="Path to pap.db (default: auto-detect from pap/config/pap.db).",
    )
    args = parser.parse_args()

    db_path   = args.db or _default_db()
    yaml_path = Path(__file__).resolve().parent / "policy_messages.yaml"

    if not yaml_path.exists():
        print(f"ERROR: policy_messages.yaml not found at {yaml_path}")
        sys.exit(1)

    if not db_path.exists():
        print(f"ERROR: database not found at {db_path}")
        sys.exit(1)

    apply(db_path=db_path, yaml_path=yaml_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
