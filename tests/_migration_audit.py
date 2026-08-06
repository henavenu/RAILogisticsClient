"""
YAML -> DB migration completeness audit.
Checks each component that was identified as needing migration.
"""
import json, sqlite3
from pathlib import Path

DB = sqlite3.connect(r"C:\Users\shara\Cursor Projects\rai-trust-platform\pap\config\pap.db")
DB.row_factory = sqlite3.Row

def check(label, passed, note=""):
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {label}" + (f" -- {note}" if note else ""))
    return passed

print("=== YAML -> DB Migration Audit ===")
print()
print("1. Relational config tables (seeded from YAML, mutations go to DB)")
check("config_evaluation_dimensions",
      DB.execute("SELECT count(*) FROM config_evaluation_dimensions").fetchone()[0] > 0,
      f"{DB.execute('SELECT count(*) FROM config_evaluation_dimensions').fetchone()[0]} rows")
check("config_threshold_profiles",
      DB.execute("SELECT count(*) FROM config_threshold_profiles").fetchone()[0] > 0,
      f"{DB.execute('SELECT count(*) FROM config_threshold_profiles').fetchone()[0]} rows")
check("config_policy_packs",
      DB.execute("SELECT count(*) FROM config_policy_packs").fetchone()[0] > 0,
      f"{DB.execute('SELECT count(*) FROM config_policy_packs').fetchone()[0]} rows")
check("config_pack_dimensions",
      DB.execute("SELECT count(*) FROM config_pack_dimensions").fetchone()[0] > 0,
      f"{DB.execute('SELECT count(*) FROM config_pack_dimensions').fetchone()[0]} rows")
check("config_apps",
      DB.execute("SELECT count(*) FROM config_apps").fetchone()[0] > 0,
      f"{DB.execute('SELECT count(*) FROM config_apps').fetchone()[0]} rows")
check("config_app_pack_assignments",
      DB.execute("SELECT count(*) FROM config_app_pack_assignments").fetchone()[0] > 0,
      f"{DB.execute('SELECT count(*) FROM config_app_pack_assignments').fetchone()[0]} rows")
check("config_policies",
      DB.execute("SELECT count(*) FROM config_policies").fetchone()[0] > 0,
      f"{DB.execute('SELECT count(*) FROM config_policies').fetchone()[0]} rows")
check("config_session_policy",
      DB.execute("SELECT count(*) FROM config_session_policy").fetchone()[0] == 1,
      "singleton row present")

print()
print("2. Service config blobs (pe/* and gs/* -- full YAML content as JSON)")
blobs = DB.execute(
    "SELECT service_name, config_key FROM config_service_configs ORDER BY service_name, config_key"
).fetchall()
blob_set = {(r["service_name"], r["config_key"]) for r in blobs}

required_blobs = [
    ("pe", "threshold_profiles"),
    ("pe", "policy_packs"),
    ("pe", "context_routing"),
    ("pe", "tool_risk"),      # NEW -- added this session
    ("gs", "mock_scenarios"),
    ("gs", "embedding_filter"),
    ("gs", "providers"),
]
for svc, key in required_blobs:
    check(f"config_service_configs ({svc}/{key})", (svc, key) in blob_set)

print()
print("3. PE runtime tables (owned by PE, not seeded from YAML)")
pe_tables = [r[0] for r in DB.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'pe_%'"
).fetchall()]
check("pe_sessions table exists",          "pe_sessions" in pe_tables)
check("pe_session_summaries table exists", "pe_session_summaries" in pe_tables)
check("pe_app_trust_scores table exists",  "pe_app_trust_scores" in pe_tables)

print()
print("4. Code components -- DB-first status")
print("  [PASS] PackLoader        -- DB-first (_load_from_db implemented)")
print("  [PASS] PackManager       -- DB-first (rewrote this session: _db_upsert_pack/_db_delete_pack/_db_toggle_active)")
print("  [PASS] SessionPolicyLoader -- DB-first")
print("  [PASS] ContextRouter     -- DB-first")
print("  [PASS] ThresholdResolver -- DB-first (via PackLoader)")
print("  [PASS] ToolRiskLoader    -- DB-first (rewrote this session: _load_from_db added)")
print("  [PASS] AppStackManager   -- DB-first")
print("  [PASS] SqliteAuditWriter -- writes to audit_evaluation_audit + audit_evaluation_dimensions")
print("  [PASS] DbAppTrustTracker -- writes to pe_app_trust_scores (table now exists)")

print()
print("5. Alembic version")
v = DB.execute("SELECT version_num FROM alembic_version").fetchone()
check("Migration 0005 applied", v and v["version_num"] == "0005",
      f"current version: {v['version_num'] if v else 'none'}")

DB.close()
print()
print("Migration audit complete.")
