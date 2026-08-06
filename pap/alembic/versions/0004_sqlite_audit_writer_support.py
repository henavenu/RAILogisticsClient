"""SQLite audit writer support

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-23

Adds a composite UNIQUE index on audit_evaluation_dimensions(evaluation_audit_id, dimension_id)
so the SqliteAuditWriter in the Policy Engine can use INSERT OR IGNORE for idempotent
dimension inserts.  Without this index the INSERT OR IGNORE degrades to INSERT INTO,
which is functionally correct but risks duplicate rows on PE retry.

Also ensures the audit_evaluation_audit and audit_evaluation_dimensions tables exist
(created by create_all on first run; this migration is a no-op if they're already there).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add UNIQUE index on (evaluation_audit_id, dimension_id) for idempotent
    # INSERT OR IGNORE from SqliteAuditWriter.
    # CREATE INDEX IF NOT EXISTS is safe to run multiple times.
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_dim_per_audit
        ON audit_evaluation_dimensions (evaluation_audit_id, dimension_id)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_dim_per_audit")
