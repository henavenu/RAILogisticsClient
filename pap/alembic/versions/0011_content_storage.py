"""Content storage for analytics drill-down

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-06

Three targeted changes to support the analytics drill-down flow
(App → Dimension Tiles → Evaluations → Evaluation Trail):

1. ADD COLUMN content_snippet TEXT to audit_evaluation_audit
   Stores the first CONTENT_SNIPPET_MAX_CHARS characters of the evaluated
   content (prompt or response).  Populated by SqliteAuditWriter when
   STORE_CONTENT_SNIPPETS=true (env var, default false).
   NULL when content capture is disabled.

2. ADD COLUMN content_hash VARCHAR(64) to audit_evaluation_audit
   SHA-256 hex digest of the *full* content (not just the snippet).
   Allows deduplication and Arize look-up even when snippet is truncated.
   Populated alongside content_snippet when STORE_CONTENT_SNIPPETS=true.

3. ADD COLUMN arize_trace_url VARCHAR(512) to audit_evaluation_audit
   Placeholder for the Arize Phoenix deep-link URL that maps this
   interaction_id to the corresponding LLM trace.  NULL until the Arize
   integration layer (future phase) populates it via PATCH.

4. CREATE INDEX idx_audit_function_name
   Functional index on json_extract(metadata_json, '$.function_name') so
   the analytics endpoint can efficiently group evaluations by the
   originating tool/function name without a full table scan.

Safe to re-run: ALTER TABLE IF NOT EXISTS is not available in SQLite, but
op.execute() calls are wrapped in try/except inside upgrade() so re-running
the migration after a partial failure is always safe.

Note on SQLite ALTER TABLE:
  SQLite only supports ADD COLUMN (not MODIFY COLUMN or ADD CONSTRAINT).
  Columns are added with no DEFAULT so existing rows get NULL — correct
  because content capture was not active for historical records.
"""
from alembic import op


revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. content_snippet ───────────────────────────────────────────────────────
    # Use try/except: SQLite returns OperationalError "duplicate column name" if
    # this migration is re-applied (e.g. after a DB reset that preserved rows).
    try:
        op.execute(
            "ALTER TABLE audit_evaluation_audit "
            "ADD COLUMN content_snippet TEXT"
        )
    except Exception:
        pass  # Column already exists — idempotent

    # ── 2. content_hash ───────────────────────────────────────────────────────────
    try:
        op.execute(
            "ALTER TABLE audit_evaluation_audit "
            "ADD COLUMN content_hash VARCHAR(64)"
        )
    except Exception:
        pass

    # ── 3. arize_trace_url ────────────────────────────────────────────────────────
    try:
        op.execute(
            "ALTER TABLE audit_evaluation_audit "
            "ADD COLUMN arize_trace_url VARCHAR(512)"
        )
    except Exception:
        pass

    # ── 4. Index on function_name inside metadata_json ───────────────────────────
    # Allows efficient GROUP BY function_name queries in the analytics API.
    # SQLite supports expression indexes from version 3.38.0 (2022-02-22);
    # CREATE INDEX IF NOT EXISTS guards against re-runs.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_function_name
        ON audit_evaluation_audit (
            json_extract(metadata_json, '$.function_name')
        )
    """)


def downgrade() -> None:
    # SQLite does not support DROP COLUMN in versions prior to 3.35.0.
    # For portability, downgrade is implemented as a no-op — the columns
    # are nullable and harmless.  The index IS droppable on all versions.
    op.execute("DROP INDEX IF EXISTS idx_audit_function_name")
    # To fully revert on SQLite ≥ 3.35.0, uncomment:
    # op.execute("ALTER TABLE audit_evaluation_audit DROP COLUMN content_snippet")
    # op.execute("ALTER TABLE audit_evaluation_audit DROP COLUMN content_hash")
    # op.execute("ALTER TABLE audit_evaluation_audit DROP COLUMN arize_trace_url")
