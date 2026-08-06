import sqlite3
conn = sqlite3.connect(r"C:\Users\shara\Cursor Projects\rai-trust-platform\pap\config\pap.db")
tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
print("\n".join(tables))
conn.close()
