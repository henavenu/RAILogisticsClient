#!/usr/bin/env python3
"""
One-shot helper -- seed all config_service_configs blobs from YAML source files.

Run after migration 0005 has been applied.  Safe to re-run (idempotent).

Blobs seeded:
  pe / threshold_profiles  -- threshold_profiles.yaml
  pe / policy_packs        -- policy_packs.yaml
  pe / context_routing     -- context_routing.yaml
  pe / tool_risk           -- tool_risk_registry.yaml
  gs / mock_scenarios      -- mock_scenarios.yaml
  gs / embedding_filter    -- embedding_filter.yaml
  gs / providers           -- providers.yaml

Usage:
    cd rai-trust-platform
    python tests/seed_service_configs.py
"""
import json
import sqlite3
from pathlib import Path

import yaml

_ROOT     = Path(__file__).resolve().parent.parent
DB_PATH   = _ROOT / "pap" / "config" / "pap.db"
_PE       = _ROOT / "policy-engine" / "config"
_GS       = _ROOT / "guardrail-service" / "config"

BLOBS = [
    ("pe", "threshold_profiles", _PE / "threshold_profiles.yaml"),
    ("pe", "policy_packs",       _PE / "policy_packs.yaml"),
    ("pe", "context_routing",    _PE / "context_routing.yaml"),
    ("pe", "tool_risk",          _PE / "tool_risk_registry.yaml"),
    ("gs", "mock_scenarios",     _GS / "mock_scenarios.yaml"),
    ("gs", "embedding_filter",   _GS / "embedding_filter.yaml"),
    ("gs", "providers",          _GS / "providers.yaml"),
]

if not DB_PATH.exists():
    raise FileNotFoundError(f"pap.db not found at {DB_PATH}")

conn = sqlite3.connect(str(DB_PATH))
conn.row_factory = sqlite3.Row

existing = {
    (r["service_name"], r["config_key"])
    for r in conn.execute(
        "SELECT service_name, config_key FROM config_service_configs"
    ).fetchall()
}

seeded = 0
skipped = 0

for service_name, config_key, path in BLOBS:
    if (service_name, config_key) in existing:
        print(f"  SKIP   {service_name}/{config_key} -- already present")
        skipped += 1
        continue

    if not path.exists():
        print(f"  WARN   {service_name}/{config_key} -- source file not found: {path}")
        skipped += 1
        continue

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    conn.execute(
        """
        INSERT INTO config_service_configs
            (service_name, config_key, config_json, seeded_from, created_at, updated_at)
        VALUES
            (?, ?, ?, ?, datetime('now'), datetime('now'))
        """,
        (service_name, config_key, json.dumps(data), str(path))
    )
    print(f"  SEEDED {service_name}/{config_key} from {path.name}")
    seeded += 1

if seeded:
    conn.commit()

conn.close()
print()
print(f"Done -- seeded {seeded}, skipped {skipped}.")
if seeded:
    print("Restart PE and GS to load configs from DB.")
