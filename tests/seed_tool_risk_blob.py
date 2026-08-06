#!/usr/bin/env python3
"""
One-shot helper -- seed the pe/tool_risk service-config blob from
tool_risk_registry.yaml into config_service_configs.

Run once; safe to re-run (idempotent -- skips if blob already present).

Usage:
    cd rai-trust-platform
    python tests/seed_tool_risk_blob.py
"""
import json
import sqlite3
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
DB_PATH   = _ROOT / "pap" / "config" / "pap.db"
YAML_PATH = _ROOT / "policy-engine" / "config" / "tool_risk_registry.yaml"

if not DB_PATH.exists():
    raise FileNotFoundError(f"pap.db not found at {DB_PATH}")
if not YAML_PATH.exists():
    raise FileNotFoundError(f"tool_risk_registry.yaml not found at {YAML_PATH}")

import yaml
with open(YAML_PATH, encoding="utf-8") as f:
    data = yaml.safe_load(f) or {}

conn = sqlite3.connect(str(DB_PATH))
conn.row_factory = sqlite3.Row

row = conn.execute(
    "SELECT config_key FROM config_service_configs "
    "WHERE service_name = 'pe' AND config_key = 'tool_risk'"
).fetchone()

if row:
    print("pe/tool_risk blob already present in config_service_configs -- no change needed.")
else:
    conn.execute(
        """
        INSERT INTO config_service_configs
            (service_name, config_key, config_json, seeded_from, created_at, updated_at)
        VALUES
            ('pe', 'tool_risk', ?, ?, datetime('now'), datetime('now'))
        """,
        (json.dumps(data), str(YAML_PATH))
    )
    conn.commit()
    tool_count = len(data.get("tools", {}))
    print(f"Seeded pe/tool_risk blob with {tool_count} tools into config_service_configs.")
    print("Done. Restart the Policy Engine to load tool risks from DB.")

conn.close()
