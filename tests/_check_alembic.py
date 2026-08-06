import sqlite3
conn = sqlite3.connect(r"C:\Users\shara\Cursor Projects\rai-trust-platform\pap\config\pap.db")
row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
print("Current alembic version:", row[0] if row else "none")
conn.close()
