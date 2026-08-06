"""Add policy_code, normative_action, normative_guidance to config_policies

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-28

Adds three columns that feed the MessageWriter component:

  policy_code       — standardised policy identifier (TP-CS-001 etc.)
                      Used as the bullet-point label in rendered messages.

  normative_action  — 'HITL' or 'REVISION' (default 'REVISION').
                      Determines whether a NORMATIVE verdict pauses the session
                      for human review or instructs the AI/agent to self-correct.

  normative_guidance — actionable text injected into the normative message:
                       • REVISION → "Remove or rephrase all discriminatory …"
                       • HITL     → "Referred to Compliance for suitability review …"
                       Stored separately from remediation_normative (the detection
                       description line) so the MessageWriter can compose the full
                       structured message from discrete content fragments.
"""
from alembic import op
import sqlalchemy as sa

revision      = '0017'
down_revision = '0016'
branch_labels = None
depends_on    = None


def upgrade() -> None:
    with op.batch_alter_table('config_policies') as batch_op:
        batch_op.add_column(sa.Column(
            'policy_code',
            sa.String(20),
            nullable=True,
            comment='Standardised policy ID — TP-{CAT}-{NNN}',
        ))
        batch_op.add_column(sa.Column(
            'normative_action',
            sa.String(10),
            nullable=False,
            server_default='REVISION',
            comment='HITL | REVISION — action taken on a NORMATIVE verdict',
        ))
        batch_op.add_column(sa.Column(
            'normative_guidance',
            sa.Text,
            nullable=True,
            comment=(
                'Actionable guidance injected into the normative message. '
                'REVISION: revision instruction for the AI/agent. '
                'HITL: status line shown to the operator.'
            ),
        ))


def downgrade() -> None:
    with op.batch_alter_table('config_policies') as batch_op:
        batch_op.drop_column('normative_guidance')
        batch_op.drop_column('normative_action')
        batch_op.drop_column('policy_code')
