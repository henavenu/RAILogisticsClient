"""Quick schema verification — lists all tables and checks expected count."""
import sqlite3

EXPECTED = {
    # Config tier
    "config_evaluation_dimensions",
    "config_threshold_profiles",
    "config_policy_packs",
    "config_pack_dimensions",
    "config_apps",
    "config_app_pack_assignments",
    "config_dimension_overrides",
    "config_policies",
    "config_session_policy",
    "config_app_trust_weights",
    "config_model_pricing",
    "config_policy_templates",
    # Audit tier
    "audit_pap_log",
    "audit_evaluation_audit",
    "audit_evaluation_dimensions",
    "audit_log",
    # T2 proxy
    "tier2_eval_scores",
    # T3 alerts (events, kept)
    "trust_score_alerts",
    # Generic metrics store (replaces trust_score_snapshots + agent_trust_scores)
    "metric_snapshots",
    # Alembic version tracking
    "alembic_version",
}

db = sqlite3.connect("config/pap.db")
rows = db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
db.close()

found = {r[0] for r in rows}
print(f"Tables in DB : {len(found)}")
for name in sorted(found):
    tag = "" if name in EXPECTED else "  <-- UNEXPECTED"
    print(f"  {name}{tag}")

missing = EXPECTED - found
if missing:
    print(f"\nMISSING ({len(missing)}):")
    for name in sorted(missing):
        print(f"  {name}")
else:
    print("\nAll expected tables present.")

# Check new columns on audit_evaluation_audit
db = sqlite3.connect("config/pap.db")
cols = {r[1] for r in db.execute("PRAGMA table_info(audit_evaluation_audit)").fetchall()}
db.close()
new_cols = {"enterprise_id", "model_id", "input_tokens", "output_tokens"}
missing_cols = new_cols - cols
if missing_cols:
    print(f"\nMissing columns on audit_evaluation_audit: {missing_cols}")
else:
    print("audit_evaluation_audit new columns       : OK")

# Check new columns on audit_evaluation_dimensions
db = sqlite3.connect("config/pap.db")
cols = {r[1] for r in db.execute("PRAGMA table_info(audit_evaluation_dimensions)").fetchall()}
db.close()
new_cols = {"score_normalized", "is_violation", "policy_id"}
missing_cols = new_cols - cols
if missing_cols:
    print(f"Missing columns on audit_evaluation_dimensions: {missing_cols}")
else:
    print("audit_evaluation_dimensions new columns  : OK")

# Check metric_snapshots columns
db = sqlite3.connect("config/pap.db")
cols = {r[1] for r in db.execute("PRAGMA table_info(metric_snapshots)").fetchall()}
db.close()
expected_cols = {"id", "metric_name", "labels", "value", "window_type", "window_start", "computed_at"}
missing_cols = expected_cols - cols
if missing_cols:
    print(f"Missing columns on metric_snapshots: {missing_cols}")
else:
    print("metric_snapshots columns                 : OK")

print(f"\nAlembic revision:")
db = sqlite3.connect("config/pap.db")
rev = db.execute("SELECT version_num FROM alembic_version").fetchone()
db.close()
print(f"  {rev[0] if rev else 'none'}")
