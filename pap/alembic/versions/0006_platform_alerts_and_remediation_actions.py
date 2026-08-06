"""Add platform_alerts table and remediation_actions to config_policies

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-28

Changes:
  1. platform_alerts — unified alert store for HITL and operational alerts.
       category distinguishes HITL items from provider/threshold alerts.
       Replaces the need for a separate HITL queue table; the PAP dashboard
       renders filtered views of this table.

  2. config_policies.remediation_actions — JSON array of structured machine-
       actionable instructions alongside the existing plain-text remediation
       fields.  Supports hitl_platform and hitl_app action types.
"""
from alembic import op
import sqlalchemy as sa


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. platform_alerts ────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS platform_alerts (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,

            -- Category distinguishes alert types for dashboard filtering
            -- Values: HITL | PROVIDER_UNAVAILABLE | THRESHOLD_BREACH | EVAL_ANOMALY
            category         VARCHAR(32)  NOT NULL,

            -- HIGH | URGENT | MEDIUM | INFO
            severity         VARCHAR(16)  NOT NULL DEFAULT 'HIGH',

            -- Source context (all optional — provider alerts have no interaction)
            app_id           VARCHAR(128),
            interaction_id   VARCHAR(128),   -- FK-by-convention to audit_evaluation_audit
            pack_id          VARCHAR(64),
            dimension        VARCHAR(64),
            provider_name    VARCHAR(64),    -- populated for PROVIDER_UNAVAILABLE

            -- Full structured context for the reviewer / ops dashboard
            payload          JSON,

            -- Lifecycle
            triggered_at     DATETIME NOT NULL DEFAULT (datetime('now')),
            resolved_at      DATETIME,
            resolved_by      VARCHAR(128),
            -- acknowledged | override | escalated | auto_resolved
            resolution       VARCHAR(32),
            resolution_note  TEXT
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_platform_alerts_category
        ON platform_alerts (category)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_platform_alerts_unresolved
        ON platform_alerts (category, resolved_at)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_platform_alerts_app_id
        ON platform_alerts (app_id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_platform_alerts_interaction
        ON platform_alerts (interaction_id)
    """)

    # ── 2. remediation_actions on config_policies ─────────────────────────────
    # JSON array of structured action objects.  Each object has at minimum:
    #   { "type": "hitl_platform" | "hitl_app", "priority": "HIGH" | "URGENT" | "MEDIUM" }
    # hitl_platform → PE writes a platform_alerts row (category=HITL)
    # hitl_app      → PE includes the action in the verdict response for the SDK
    # Both can appear together on the same policy for fan-out cases.
    op.execute("""
        ALTER TABLE config_policies
        ADD COLUMN remediation_actions JSON
    """)


def downgrade() -> None:
    # SQLite does not support DROP COLUMN — recreate without the column if needed
    op.execute("DROP INDEX IF EXISTS ix_platform_alerts_interaction")
    op.execute("DROP INDEX IF EXISTS ix_platform_alerts_app_id")
    op.execute("DROP INDEX IF EXISTS ix_platform_alerts_unresolved")
    op.execute("DROP INDEX IF EXISTS ix_platform_alerts_category")
    op.execute("DROP TABLE IF EXISTS platform_alerts")
    # Note: removing the remediation_actions column from config_policies
    # requires a full table rebuild in SQLite — skipped in downgrade for safety.
    # In production (Postgres) use: op.drop_column('config_policies', 'remediation_actions')
