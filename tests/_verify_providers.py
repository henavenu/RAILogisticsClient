import sqlite3, json
conn = sqlite3.connect(r"C:\Users\shara\Cursor Projects\rai-trust-platform\pap\config\pap.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT provider_id, provider_type, active, json_array_length(supported_dims) as dim_count, "
    "health_status FROM config_providers ORDER BY provider_type, provider_id"
).fetchall()
print(f"{'provider_id':<30} {'type':<12} {'active':<8} {'dims':<6} {'health'}")
print("-" * 72)
for r in rows:
    print(f"{r['provider_id']:<30} {r['provider_type']:<12} {str(bool(r['active'])):<8} {r['dim_count']:<6} {r['health_status']}")
conn.close()
