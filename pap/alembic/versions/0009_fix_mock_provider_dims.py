"""Fix mock_provider supported_dims + add pii_exposure_flag dimension

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-05

Two targeted data fixes:

1. mock_provider supported_dims → [] (universal fallback)
   The seed incorrectly declared a restrictive supported_dims list for the
   mock provider, blocking assignment to dimensions like data_leakage.
   Mock Provider is the universal fallback: empty list = no filter = accepts
   any dimension.  The capability gate (server + client) already skips the
   check when supported_dims is empty, so clearing it is sufficient.

2. Add pii_exposure_flag evaluation dimension (security category)
   The T1 assurance metric pii_exposure_flag maps to data_leakage verdicts
   but had no corresponding entry in the evaluation dimension catalogue,
   so it was invisible in the Evaluations tab.  Adding it here makes the
   dimension visible and assignable; provider defaults to mock_provider.
   The dimension is seeded as 'deferred' (observability-only) because
   the primary enforcement path is data_leakage — this entry surfaces the
   PII signal in the taxonomy without introducing duplicate enforcement.
"""
from datetime import datetime

from alembic import op


revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Clear mock_provider capability filter ──────────────────────────────
    # supported_dims = '[]' (empty JSON array) means no restriction —
    # the provider will be offered as an option for every dimension.
    op.execute("""
        UPDATE config_providers
        SET    supported_dims = '[]',
               updated_at     = datetime('now')
        WHERE  provider_id = 'mock_provider'
    """)

    # ── 2. Add pii_exposure_flag evaluation dimension ─────────────────────────
    # Only insert if not already present (idempotent).
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    desc = (
        "Detects prompts or responses that trigger PII / data-leakage signals "
        "captured by the Privacy Proxy and Policy Engine. "
        "Maps to data_leakage enforcement verdicts. "
        "Observability dimension - scored by mock_provider in dev; "
        "assign aws_bedrock_guardrail for production enforcement."
    )
    op.execute(f"""
        INSERT OR IGNORE INTO config_evaluation_dimensions
            (dimension_id, category, display_name, description,
             evaluator_type, provider, status, supported_roles,
             requires_context, created_at, updated_at)
        VALUES
            ('pii_exposure_flag', 'security',
             'PII Exposure Flag',
             '{desc}',
             'gs_call', 'mock_provider', 'deferred',
             '["user_prompt","llm_response","agent_to_agent"]',
             0, '{now}', '{now}')
    """)


def downgrade() -> None:
    # Restore the original supported_dims list for mock_provider
    original = (
        '["sanctions_country_reference","sanctions_entity_mention",'
        '"sanctions_evasion_signal","aml_structuring_signal",'
        '"eu_ai_act_transparency","eu_ai_act_prohibited_practice",'
        '"eu_ai_act_bias_discrimination"]'
    )
    op.execute(f"""
        UPDATE config_providers
        SET    supported_dims = '{original}',
               updated_at     = datetime('now')
        WHERE  provider_id = 'mock_provider'
    """)

    op.execute("""
        DELETE FROM config_evaluation_dimensions
        WHERE  dimension_id = 'pii_exposure_flag'
    """)
