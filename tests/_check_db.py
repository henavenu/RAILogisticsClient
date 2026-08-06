import sqlite3, sys
sys.stdout.reconfigure(encoding='utf-8')
db = r'C:\Users\shara\Cursor Projects\rai-trust-platform\pap\config\pap.db'
con = sqlite3.connect(db)

tables = con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
print("Tables:", [t[0] for t in tables])

try:
    cols = con.execute("PRAGMA table_info(config_evaluation_dimensions)").fetchall()
    print("CED columns:", [c[1] for c in cols])
except Exception as e:
    print("Error:", e)

# Check zero_tolerance column
try:
    rows = con.execute("SELECT dimension_id, zero_tolerance FROM config_evaluation_dimensions WHERE zero_tolerance = 1").fetchall()
    print("ZT dims:", rows)
except Exception as e:
    print("ZT query error:", e)

# Check governance_alerts table
try:
    rows = con.execute("SELECT id, tier, dimension, session_id FROM governance_alerts ORDER BY id DESC LIMIT 10").fetchall()
    print("Gov alerts:", rows)
except Exception as e:
    print("Gov alerts error:", e)

con.close()
