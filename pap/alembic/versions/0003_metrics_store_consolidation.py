"""Metrics store consolidation

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-22

Replaces the per-metric T3 tables added in 0002 with a single generic
metric_snapshots table (Prometheus recording-rule pattern).

Dropped tables (replaced by metric_snapshots):
  trust_score_snapshots  — per-app composite score snapshots
  agent_trust_scores     — rolling per-agent score (fits generic shape)

Kept tables (events, not metrics — need resolution lifecycle):
  trust_score_alerts     — SRE burn-rate breach records (kept as-is)

Kept tables (reference data, not metrics):
  config_app_trust_weights  — weighting formula config (kept as-is)
  tier2_eval_scores         — T2 proxy (kept as-is — raw eval data, not metrics)
  cogs_records              — dropped (COGS computed on demand from Phoenix + pricing)

New table:
  metric_snapshots  — generic time-series metric store (Prometheus label model)

  Every platform metric is stored as:
    metric_name  — e.g. 'app_trust_score', 'pii_exposure_rate', 'latency_p95_ms'
    labels       — JSON label set, e.g. {"app_id": "mybank", "role": "tool_call"}
    value        — float metric value
    window_type  — time window the value covers (1h | 6h | 24h | 7d | 30d | point)
    window_start — start of the window
    computed_at  — when this snapshot was materialised

  This design:
    - Prevents per-metric table sprawl
    - Mirrors the Prometheus/OTel Metrics data model
    - Swaps cleanly to Arize GraphQL Metrics API in production via TrustDataProvider SPI
    - Allows any new metric to be added without a schema migration
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:

    # ── Drop replaced tables ──────────────────────────────────────────────────
    op.drop_table("trust_score_snapshots")
    op.drop_table("agent_trust_scores")
    op.drop_table("cogs_records")          # COGS computed on demand from Phoenix

    # ── New: metric_snapshots ─────────────────────────────────────────────────
    op.create_table(
        "metric_snapshots",
        sa.Column("id",           sa.Integer,    primary_key=True, autoincrement=True),

        # Metric identity
        # Examples: app_trust_score | pii_exposure_rate | latency_p95_ms |
        #           token_count_prompt | token_count_completion | violation_rate
        sa.Column("metric_name",  sa.String(128), nullable=False),

        # Label set — JSON object mirroring Prometheus label model
        # Common keys: app_id, enterprise_id, role, dimension_id, model_id, eval_mode
        # Kept as JSON so any new dimension is a data change, not a schema migration
        sa.Column("labels",       sa.JSON,        nullable=False, server_default="{}"),

        # The metric value
        sa.Column("value",        sa.Float,       nullable=False),

        # Time window this value covers
        # Use 'point' for instantaneous readings, '1h'/'6h'/'24h'/'7d'/'30d' for aggregates
        sa.Column("window_type",  sa.String(8),   nullable=False, server_default="point"),
        sa.Column("window_start", sa.DateTime,    nullable=False),

        # When this row was materialised by the compute job
        sa.Column("computed_at",  sa.DateTime,    nullable=False),
    )
    op.create_index(
        "ix_metric_name_labels",
        "metric_snapshots",
        ["metric_name", "window_type", "window_start"],
    )
    op.create_index(
        "ix_metric_computed_at",
        "metric_snapshots",
        ["computed_at"],
    )


def downgrade() -> None:
    op.drop_table("metric_snapshots")

    # Re-create dropped tables (minimal — data is not restored)
    op.create_table(
        "cogs_records",
        sa.Column("id",                  sa.Integer,    primary_key=True, autoincrement=True),
        sa.Column("interaction_id",      sa.String(36),  nullable=False),
        sa.Column("app_id",              sa.String(128), nullable=False),
        sa.Column("enterprise_id",       sa.String(128), nullable=True),
        sa.Column("provider",            sa.String(64),  nullable=False),
        sa.Column("model_id",            sa.String(128), nullable=False),
        sa.Column("input_tokens",        sa.Integer,     nullable=False, server_default="0"),
        sa.Column("output_tokens",       sa.Integer,     nullable=False, server_default="0"),
        sa.Column("cost_per_1m_input",   sa.Float,       nullable=True),
        sa.Column("cost_per_1m_output",  sa.Float,       nullable=True),
        sa.Column("total_cost_usd",      sa.Float,       nullable=False, server_default="0.0"),
        sa.Column("timestamp",           sa.DateTime,    nullable=False),
    )

    op.create_table(
        "agent_trust_scores",
        sa.Column("id",                sa.Integer,    primary_key=True, autoincrement=True),
        sa.Column("enterprise_id",     sa.String(128), nullable=True),
        sa.Column("app_id",            sa.String(128), nullable=False),
        sa.Column("agent_id",          sa.String(128), nullable=False),
        sa.Column("trust_score",       sa.Float,       nullable=False, server_default="1.0"),
        sa.Column("score_safety",      sa.Float,       nullable=True),
        sa.Column("score_security",    sa.Float,       nullable=True),
        sa.Column("score_compliance",  sa.Float,       nullable=True),
        sa.Column("total_evaluations", sa.Integer,     nullable=False, server_default="0"),
        sa.Column("total_violations",  sa.Integer,     nullable=False, server_default="0"),
        sa.Column("last_violation_at", sa.DateTime,    nullable=True),
        sa.Column("first_seen_at",     sa.DateTime,    nullable=False),
        sa.Column("last_updated_at",   sa.DateTime,    nullable=False),
        sa.UniqueConstraint("enterprise_id", "app_id", "agent_id", name="uq_agent_trust"),
    )

    op.create_table(
        "trust_score_snapshots",
        sa.Column("id",                sa.Integer,    primary_key=True, autoincrement=True),
        sa.Column("enterprise_id",     sa.String(128), nullable=True),
        sa.Column("app_id",            sa.String(128), nullable=True),
        sa.Column("agent_id",          sa.String(128), nullable=True),
        sa.Column("window_type",       sa.String(8),  nullable=False),
        sa.Column("window_start",      sa.DateTime,   nullable=False),
        sa.Column("window_end",        sa.DateTime,   nullable=False),
        sa.Column("trust_score",       sa.Float,      nullable=False),
        sa.Column("score_safety",      sa.Float,      nullable=True),
        sa.Column("score_security",    sa.Float,      nullable=True),
        sa.Column("score_compliance",  sa.Float,      nullable=True),
        sa.Column("score_operational", sa.Float,      nullable=True),
        sa.Column("score_quality",     sa.Float,      nullable=True),
        sa.Column("total_evaluations", sa.Integer,    nullable=False, server_default="0"),
        sa.Column("total_violations",  sa.Integer,    nullable=False, server_default="0"),
        sa.Column("violation_rate",    sa.Float,      nullable=True),
        sa.Column("score_delta",       sa.Float,      nullable=True),
        sa.Column("burn_rate",         sa.Float,      nullable=True),
        sa.Column("computed_at",       sa.DateTime,   nullable=False),
    )
