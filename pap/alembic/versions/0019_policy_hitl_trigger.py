"""Add hitl_trigger to config_policies (per-policy human-in-the-loop)

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-16

Adds a per-POLICY HITL trigger so a single dimension's policy can require human
review independently of any pack. When hitl_trigger=True and that policy returns
a non-PERMISSIVE verdict, the Policy Engine raises a HITL interrupt — mirroring the
existing pack-level config_policy_packs.hitl_trigger, but at policy granularity.
This is what backs the "Require HITL" toggle on the Policies tab + Evaluation Catalog.
"""
from alembic import op
import sqlalchemy as sa

revision      = '0019'
down_revision = '0018'
branch_labels = None
depends_on    = None


def upgrade() -> None:
    with op.batch_alter_table('config_policies') as batch_op:
        batch_op.add_column(sa.Column(
            'hitl_trigger',
            sa.Boolean,
            nullable=False,
            server_default=sa.false(),
            comment='Per-policy HITL: non-PERMISSIVE verdict on this policy raises human review',
        ))


def downgrade() -> None:
    with op.batch_alter_table('config_policies') as batch_op:
        batch_op.drop_column('hitl_trigger')
