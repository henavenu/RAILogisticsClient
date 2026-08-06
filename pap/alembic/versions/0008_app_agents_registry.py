"""Add config_app_agents — agent topology registry

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-30

Each registered application's GovernedGraph writes its agent topology here
on first invocation via POST /internal/register-agents.

The PAP dashboard reads this table to show declared agents even when no
evaluation records exist yet, and to annotate observed agents with their
declared status (is_declared_agent) and known tools.

Columns:
  app_id              — application identifier (matches config_apps.app_id)
  agent_name          — LangGraph node name or a2a_tools target name
  is_declared_agent   — True if in agent_nodes or a2a_tools.values()
  declared_tools      — JSON list: tools in a2a_tools that route to this agent
  a2a_targets         — JSON list: all agent names reachable via A2A in this app
  all_nodes           — JSON list: full discovered graph topology at registration
  first_seen_at       — when first registered (SDK startup)
  last_seen_at        — most recent re-registration (SDK restart / redeploy)

UNIQUE on (app_id, agent_name) — upserted on every GovernedGraph instantiation.
"""
from alembic import op


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS config_app_agents (
            id                  INTEGER      PRIMARY KEY AUTOINCREMENT,
            app_id              VARCHAR(128) NOT NULL,
            agent_name          VARCHAR(128) NOT NULL,
            is_declared_agent   INTEGER      NOT NULL DEFAULT 0,
            declared_tools      JSON,
            a2a_targets         JSON,
            all_nodes           JSON,
            first_seen_at       DATETIME     NOT NULL DEFAULT (datetime('now')),
            last_seen_at        DATETIME     NOT NULL DEFAULT (datetime('now')),
            CONSTRAINT uq_app_agent UNIQUE (app_id, agent_name)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_config_app_agents_app_id
        ON config_app_agents (app_id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_config_app_agents_agent_name
        ON config_app_agents (agent_name)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_config_app_agents_declared
        ON config_app_agents (is_declared_agent)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_config_app_agents_declared")
    op.execute("DROP INDEX IF EXISTS ix_config_app_agents_agent_name")
    op.execute("DROP INDEX IF EXISTS ix_config_app_agents_app_id")
    op.execute("DROP TABLE IF EXISTS config_app_agents")
