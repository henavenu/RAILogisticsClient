"""Check policy thresholds for key dimensions from the PAP DB."""
import sys, io, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

db = r'C:\Users\shara\Cursor Projects\rai-trust-platform\pap\config\pap.db'
con = sqlite3.connect(db)

dims = ['jailbreak', 'prompt_injection', 'hate', 'sanctions_country_reference',
        'violence', 'sensitive_data_disclosure', 'sanctions_entity_mention', 'corporate_policy']

print("=== Default policy thresholds per dimension ===")
print(f"{'dim':35s}  {'perm_hi':8s}  {'norm_lo':8s}  {'norm_hi':8s}  {'coer_lo':8s}")
for dim in dims:
    rows = con.execute(
        "SELECT dimension, permissive_hi, normative_lo, normative_hi, coercive_lo "
        "FROM config_policies WHERE dimension = ? AND app_id IS NULL LIMIT 1", (dim,)
    ).fetchall()
    if rows:
        r = rows[0]
        print(f"{r[0]:35s}  {r[1]:8}  {r[2]:8}  {r[3]:8}  {r[4]:8}")
    else:
        print(f"{dim:35s}  (no default policy)")

# Also check app-specific overrides for mybank
print("\n=== mybank app-specific overrides ===")
rows = con.execute(
    "SELECT dimension, permissive_hi, normative_lo, normative_hi, coercive_lo "
    "FROM config_policies WHERE app_id = 'mybank'"
).fetchall()
for r in rows:
    print(f"{r[0]:35s}  perm_hi={r[1]}  norm_lo={r[2]}  norm_hi={r[3]}  coer_lo={r[4]}")

con.close()
