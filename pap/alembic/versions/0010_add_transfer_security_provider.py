"""Add transfer_security_provider to registry + update injection/jailbreak dimensions

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-06

Three targeted changes:

1. Insert transfer_security_provider into config_providers
   A local (in-process) provider with no credentials or network calls.
   Handles exactly two dimensions: prompt_injection and jailbreak.
   fail_mode = fail_open (score=1/PASS on unexpected exception).
   supported_dims explicitly set so the capability gate lets these two
   dimensions be (re-)assigned to this provider via PATCH /v1/evaluators.

2. Update config_evaluation_dimensions for prompt_injection
   - provider: aws_bedrock_guardrail  →  transfer_security_provider
   (roles and status unchanged: active on user_prompt + agent_to_agent)

3. Update config_evaluation_dimensions for jailbreak
   - provider: aws_bedrock_guardrail  →  transfer_security_provider
   - supported_roles: adds agent_to_agent (was missing from original seed)
   (status unchanged: active)

Rationale: TransferSecurityProvider is registered as a LOCAL_PROVIDER in
guardrail-service/main.py and is never replaced by mock_provider in MOCK_MODE.
Moving both dimensions to this provider eliminates the AWS Bedrock dependency
for injection/jailbreak, cuts cold-start latency to sub-millisecond, and
enables the transfer-context amplifier (+2 score when memo/beneficiary/amount
override signals co-occur) — critical for the finapp transfer_funds use case.
"""
from datetime import datetime

from alembic import op


revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    # ── 1. Insert transfer_security_provider ─────────────────────────────────────
    # INSERT OR IGNORE so re-running the migration (e.g. after a DB reset that
    # preserved the table) is safe and idempotent.
    tsp_desc = (
        "In-process prompt injection and jailbreak detection with "
        "transfer-context amplification. Covers role-boundary violation, "
        "delimiter smuggling, impersonation, constraint removal, authority "
        "claims, and persistence commands. Score amplifier (+2, capped 9) "
        "activates when memo injection / beneficiary substitution / amount "
        "override signals co-occur - purpose-built for finapp transfer_funds "
        "tool calls where the memo field is a natural injection vector. "
        "Zero external API calls; sub-millisecond latency; "
        "never replaced by mock_provider in MOCK_MODE."
    )
    op.execute(f"""
        INSERT OR IGNORE INTO config_providers
            (provider_id, provider_type, display_name, description,
             active, endpoint, api_key_env_var, extra_config,
             supported_dims, score_mapping, category_mapping,
             fail_mode, health_status, consecutive_failures,
             last_checked_at, last_failure_reason,
             created_at, updated_at)
        VALUES
            ('transfer_security_provider', 'local',
             'Transfer Security Provider',
             '{tsp_desc}',
             1, NULL, NULL, NULL,
             '["prompt_injection","jailbreak"]',
             NULL, NULL,
             'fail_open', 'healthy', 0,
             NULL, NULL,
             '{now}', '{now}')
    """)

    # ── 2. Update prompt_injection → transfer_security_provider ──────────────────
    # Supported roles were already correct (user_prompt, agent_to_agent).
    op.execute(f"""
        UPDATE config_evaluation_dimensions
        SET    provider   = 'transfer_security_provider',
               updated_at = '{now}'
        WHERE  dimension_id = 'prompt_injection'
    """)

    # ── 3. Update jailbreak → transfer_security_provider + add agent_to_agent ────
    # Original seed had only user_prompt; agent_to_agent is an equally valid
    # injection vector and is already scored by the GS since the evaluations.yaml
    # update.  This keeps the PAP catalogue in sync with the GS config.
    op.execute(f"""
        UPDATE config_evaluation_dimensions
        SET    provider        = 'transfer_security_provider',
               supported_roles = '["user_prompt","agent_to_agent"]',
               updated_at      = '{now}'
        WHERE  dimension_id = 'jailbreak'
    """)


def downgrade() -> None:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    # Restore prompt_injection and jailbreak to aws_bedrock_guardrail
    op.execute(f"""
        UPDATE config_evaluation_dimensions
        SET    provider   = 'aws_bedrock_guardrail',
               updated_at = '{now}'
        WHERE  dimension_id IN ('prompt_injection', 'jailbreak')
    """)

    # Restore jailbreak supported_roles to user_prompt only
    op.execute(f"""
        UPDATE config_evaluation_dimensions
        SET    supported_roles = '["user_prompt"]',
               updated_at      = '{now}'
        WHERE  dimension_id = 'jailbreak'
    """)

    # Remove transfer_security_provider
    op.execute("""
        DELETE FROM config_providers
        WHERE  provider_id = 'transfer_security_provider'
    """)
