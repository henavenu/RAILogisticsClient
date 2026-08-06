"""Add config_service_configs table

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-27

Adds the config_service_configs table -- a generic JSON blob store that
replaces YAML/JSON files read at PE and GS startup.  Each row is keyed by
(service_name, config_key) and stores the full parsed YAML as a JSON blob.

Blobs stored:
  ('pe', 'threshold_profiles') -- threshold_profiles.yaml (incl. step_map)
  ('pe', 'policy_packs')       -- policy_packs.yaml (incl. context_adjustments)
  ('pe', 'context_routing')    -- context_routing.yaml
  ('pe', 'tool_risk')          -- tool_risk_registry.yaml
  ('gs', 'mock_scenarios')     -- mock_scenarios.yaml
  ('gs', 'embedding_filter')   -- embedding_filter.yaml
  ('gs', 'providers')          -- providers.yaml (env-var refs only, no secrets)

After this migration, seed_service_configs() in pap/core/seed.py populates
all blobs from the YAML source files.  Services then read from DB; YAML files
are only used as a fallback when DB is unavailable.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS config_service_configs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            service_name VARCHAR(32)  NOT NULL,
            config_key   VARCHAR(64)  NOT NULL,
            config_json  JSON         NOT NULL,
            seeded_from  VARCHAR(256),
            created_at   DATETIME     NOT NULL DEFAULT (datetime('now')),
            updated_at   DATETIME     NOT NULL DEFAULT (datetime('now')),
            CONSTRAINT uq_service_config UNIQUE (service_name, config_key)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_config_service_configs_service_name
        ON config_service_configs (service_name)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_config_service_configs_service_name")
    op.execute("DROP TABLE IF EXISTS config_service_configs")
