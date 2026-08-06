"""Add supported_roles to config_providers

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-16

Adds a supported_roles JSON column to config_providers, capturing which
content-interception roles (user_prompt, llm_response, tool_call, agent_to_agent)
each provider can handle.

Together with supported_dims this gives a complete capability manifest:
  supported_dims  — which evaluation dimensions the provider scores
  supported_roles — which interception points the provider handles
  (empty list = universal / handles all)

The evaluation panel uses both axes to:
  1. Validate the current dimension→provider assignment at display time
  2. Filter the assignment dropdown to eligible providers only
  3. Warn when a dimension's configured interception points fall outside
     the assigned provider's supported_roles
"""
import json
from alembic import op
import sqlalchemy as sa

revision    = '0016'
down_revision = '0015'
branch_labels = None
depends_on    = None

# Per-provider supported_roles — empty list means "all roles" (universal).
# These represent the interception points each provider realistically handles
# given its evaluation logic and API contract.
_PROVIDER_ROLES: dict = {
    # Universal fallback — keyword-signal scorer; handles any role
    "mock_provider":              [],

    # Azure Content Safety — scores text content from users and LLM outputs
    "azure_content_safety":       ["user_prompt", "llm_response"],

    # AWS Bedrock Guardrail — input and output guardrails
    "aws_bedrock_guardrail":      ["user_prompt", "llm_response"],

    # Embedding filter — semantic off-topic / repetition on user input only
    "embedding_filter":           ["user_prompt"],

    # In-process classifier — policy keyword check on any content
    "local_policy_classifier":    ["user_prompt", "llm_response", "tool_call", "agent_to_agent"],

    # Transfer security — jailbreak / injection on user input and tool invocations
    "transfer_security_provider": ["user_prompt", "tool_call"],

    # LLM-as-judge — contextual reasoning; handles all roles
    "llm_judge":                  ["user_prompt", "llm_response", "tool_call", "agent_to_agent"],

    # Llama Prompt Guard — prompt injection / jailbreak on user inputs only
    "prompt_guard":               ["user_prompt"],
}


def upgrade() -> None:
    op.add_column(
        "config_providers",
        sa.Column("supported_roles", sa.JSON(), nullable=True),
    )
    # Back-fill known providers with their role manifests
    conn = op.get_bind()
    for pid, roles in _PROVIDER_ROLES.items():
        conn.execute(
            sa.text(
                "UPDATE config_providers SET supported_roles = :roles WHERE provider_id = :pid"
            ),
            {"roles": json.dumps(roles), "pid": pid},
        )


def downgrade() -> None:
    op.drop_column("config_providers", "supported_roles")
