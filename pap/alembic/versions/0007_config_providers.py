"""Add config_providers table

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-28

Adds config_providers — the DB-backed provider registry that replaces the
providers.yaml / config_service_configs('gs','providers') blob for runtime
provider configuration.

Each row describes one registered evaluation provider:
  - Connection config (endpoint, api_key_env_var — key NAME only, never value)
  - Capability manifest (supported_dims JSON array)
  - Score/category mappings (moved from hardcoded Python dicts)
  - Health tracking (health_status, last_checked_at, consecutive_failures)

The GS reads this table at startup and on hot-reload.  Operators manage
providers via the PAP evaluations UI (provider panel).

provider_type values:
  azure     — Azure Content Safety REST API
  aws       — AWS Bedrock Guardrail API
  sml       — Self-hosted small ML model (e.g. Llama Prompt Guard)
  llm_judge — LLM-as-judge (cloud or local; model configured per row)
  local     — In-process keyword/embedding classifier
  mock      — Mock provider for dev/test
"""
from alembic import op
import sqlalchemy as sa


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS config_providers (
            provider_id       VARCHAR(64)  PRIMARY KEY,

            -- azure | aws | sml | llm_judge | local | mock
            provider_type     VARCHAR(32)  NOT NULL,
            display_name      VARCHAR(128) NOT NULL,
            description       TEXT,

            active            INTEGER      NOT NULL DEFAULT 1,

            -- Connection (secrets NEVER stored here — only env var NAME)
            endpoint          VARCHAR(256),
            api_key_env_var   VARCHAR(64),    -- e.g. "AZURE_API_KEY"
            extra_config      JSON,           -- provider-type-specific extras
                                              -- aws: region, guardrail_arn, guardrail_version
                                              -- sml: model_path, device
                                              -- llm_judge: model_name, temperature, max_tokens

            -- Capability manifest — which canonical dims this provider can score
            supported_dims    JSON NOT NULL DEFAULT '[]',

            -- Score mapping — translates provider-native scores to canonical 1-9
            -- Format varies by provider_type:
            --   azure:     [{"range": [lo, hi], "canonical": N, "status": "PASS|WARNING|FLAGGED"}, ...]
            --   aws:       {"type": "categorical", "map": {"HIGH": {"canonical": 8, "status": "FLAGGED"}, ...}}
            --   sml:       [{"range": [lo, hi], "canonical": N, "status": "..."}]  (probability thresholds)
            --   llm_judge: inline in prompt — no mapping needed
            score_mapping     JSON,

            -- Category/label mapping — provider-native name -> canonical dimension name
            -- e.g. {"Hate": "hate", "Sexual": "sexual_content", ...}
            category_mapping  JSON,

            -- Fallback behaviour when provider is unavailable
            -- "fail_open" -> return PASS with note (default)
            -- "fail_closed" -> return FLAGGED with note
            fail_mode         VARCHAR(16)  NOT NULL DEFAULT 'fail_open',

            -- Health tracking (updated by GS on each provider call)
            -- healthy | degraded | unavailable
            health_status          VARCHAR(16)  NOT NULL DEFAULT 'healthy',
            consecutive_failures   INTEGER      NOT NULL DEFAULT 0,
            last_checked_at        DATETIME,
            last_failure_reason    TEXT,

            created_at        DATETIME NOT NULL DEFAULT (datetime('now')),
            updated_at        DATETIME NOT NULL DEFAULT (datetime('now'))
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_config_providers_active
        ON config_providers (active)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_config_providers_type
        ON config_providers (provider_type)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_config_providers_type")
    op.execute("DROP INDEX IF EXISTS ix_config_providers_active")
    op.execute("DROP TABLE IF EXISTS config_providers")
