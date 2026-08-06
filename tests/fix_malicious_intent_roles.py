#!/usr/bin/env python3
"""
One-shot fix: add 'agent_to_agent' to malicious_intent supported_roles in
config_evaluation_dimensions.

The mandatory pack lists malicious_intent with roles=user_prompt,agent_to_agent,
but the DB seeder only stored user_prompt.  This script aligns the DB with
the pack definition.

Run once; safe to re-run (idempotent).
"""
import json
import sqlite3
from pathlib import Path

DB_PATH = r"C:\Users\shara\Cursor Projects\rai-trust-platform\pap\config\pap.db"

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

row = conn.execute(
    "SELECT dimension_id, supported_roles FROM config_evaluation_dimensions "
    "WHERE dimension_id = 'malicious_intent'"
).fetchone()

if not row:
    print("ERROR: malicious_intent not found in config_evaluation_dimensions")
    conn.close()
    exit(1)

current_roles = json.loads(row["supported_roles"] or "[]")
print(f"Current roles: {current_roles}")

if "agent_to_agent" in current_roles:
    print("agent_to_agent already present — no change needed.")
else:
    updated_roles = sorted(set(current_roles) | {"agent_to_agent", "user_prompt"})
    conn.execute(
        "UPDATE config_evaluation_dimensions SET supported_roles = ? "
        "WHERE dimension_id = 'malicious_intent'",
        (json.dumps(updated_roles),),
    )
    conn.commit()
    print(f"Updated roles: {updated_roles}")
    print("Done. Restart the Guardrail Service to pick up the new role assignment.")

conn.close()
