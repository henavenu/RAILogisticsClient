"""Quick sanity check: list all config_service_configs blobs and the tables
that are supposed to be migrated away from YAML."""
import json, sqlite3
conn = sqlite3.connect(r"C:\Users\shara\Cursor Projects\rai-trust-platform\pap\config\pap.db")
conn.row_factory = sqlite3.Row

print("=== config_service_configs ===")
rows = conn.execute(
    "SELECT service_name, config_key, length(config_json) as sz, updated_at "
    "FROM config_service_configs ORDER BY service_name, config_key"
).fetchall()
for r in rows:
    print(f"  {r['service_name']}/{r['config_key']:<22}  {r['sz']:>6} bytes  ({r['updated_at']})")

print()
print("=== table row counts ===")
tables = [
    "config_policy_packs", "config_pack_dimensions",
    "config_threshold_profiles", "config_evaluation_dimensions",
    "config_app_pack_assignments", "config_apps",
    "config_policies", "config_session_policy",
]
for t in tables:
    n = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
    print(f"  {t:<40} {n:>4} rows")

conn.close()
