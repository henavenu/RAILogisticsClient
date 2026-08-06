"""Add lifecycle columns to config_app_agents (status, last_declared_at, retired_at)

Supports registry reconciliation: detect NEW agents and RETIRE stale agents
(removed from the governed app) via a full-snapshot diff on /internal/register-agents,
with a GRACE window to tolerate rolling deploys.

  status            active | observed | retired | shadow   (default: active)
  last_declared_at  last time the agent appeared in a register-agents declaration
  retired_at        when reconciliation marked it retired (NULL while live)

Revision ID: 0018
Revises: 0017
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision      = '0018'
down_revision = '0017'
branch_labels = None
depends_on    = None


def upgrade():
    op.add_column(
        'config_app_agents',
        sa.Column('status', sa.String(16), nullable=False, server_default='active'),
    )
    op.add_column(
        'config_app_agents',
        sa.Column('last_declared_at', sa.DateTime(), nullable=True),
    )
    op.add_column(
        'config_app_agents',
        sa.Column('retired_at', sa.DateTime(), nullable=True),
    )
    # Backfill: treat each existing row's last registration as its last declaration,
    # so currently-known agents are NOT immediately considered stale before the app's
    # next register-agents call refreshes them.
    op.execute(
        "UPDATE config_app_agents SET last_declared_at = last_seen_at "
        "WHERE last_declared_at IS NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_config_app_agents_status "
        "ON config_app_agents (status)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_config_app_agents_status")
    with op.batch_alter_table('config_app_agents') as batch:
        batch.drop_column('retired_at')
        batch.drop_column('last_declared_at')
        batch.drop_column('status')
