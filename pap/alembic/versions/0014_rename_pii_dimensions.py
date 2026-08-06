"""Rename data_leakage → sensitive_data_disclosure
        and pii_exposure_flag → data_minimisation_violation

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-15

Rationale
─────────
The previous dimension names carried imprecise semantics:

  data_leakage     — "leakage" implies data escaping to a log file or
                     database (an infrastructure-level event).  What we
                     actually detect is the LLM *disclosing* regulated
                     identifiers (SSN, card numbers, credentials, account
                     numbers) in its output.

  pii_exposure_flag — ambiguous; could be confused with the above since
                      both involve personal data.  What we actually detect
                      is the LLM returning *more* personal data than the
                      request required — a violation of the GDPR data
                      minimisation principle (Art. 5(1)(c)).

New names and semantics
────────────────────────
  sensitive_data_disclosure  (was: data_leakage)
    Regulated identifiers (SSN, PAN, credentials, routing/account numbers)
    appearing in LLM output.  Maps to OWASP LLM02.
    Track: COERCIVE — hard block.  PE returns safe refusal.
    Category: security  (moved from privacy)

  data_minimisation_violation  (was: pii_exposure_flag)
    LLM returns correct but excessive personal data (address + phone + DOB
    + email when only name was needed).  Violates GDPR Art. 5(1)(c) —
    data must be "adequate, relevant and limited to what is necessary."
    Track: NORMATIVE — PE field-masks specific entities before delivery.
    Category: compliance  (moved from privacy)

Tables updated
──────────────
  config_evaluation_dimensions  — dimension_id (PK) + category + description
  config_pack_dimensions        — dimension_id FK
  config_dimension_overrides    — dimension_id FK
  config_policies               — dimension_id FK
  audit_evaluation_dimensions   — dimension_id (historical records)

Downgrade restores all previous values exactly.
"""
from datetime import datetime

from alembic import op

revision    = "0014"
down_revision = "0013"
branch_labels = None
depends_on  = None

# ── Rename pairs ───────────────────────────────────────────────────────────────
#   (old_id, new_id, new_category, new_description)
_RENAMES = [
    (
        "data_leakage",
        "sensitive_data_disclosure",
        "security",
        (
            "LLM response discloses regulated identifiers (SSN, card numbers, "
            "credentials, account numbers) that must never appear in output. "
            "Maps to OWASP LLM02 — Sensitive Information Disclosure. "
            "COERCIVE threshold: PE hard-blocks response and returns safe refusal."
        ),
    ),
    (
        "pii_exposure_flag",
        "data_minimisation_violation",
        "compliance",
        (
            "LLM response returns more personal data than necessary for the request "
            "(e.g. full address + phone + DOB + email when only name was needed). "
            "Violates GDPR Art. 5(1)(c) data minimisation principle. "
            "NORMATIVE threshold: PE field-masks specific entities before delivery. "
            "Audit log stores both original and sanitised versions."
        ),
    ),
]

# Tables where the FK column is named dimension_id
_FK_DIM_ID_TABLES = [
    "config_pack_dimensions",
    "config_dimension_overrides",
    "audit_evaluation_dimensions",
]

# config_policies uses column named 'dimension' (historical naming, not dimension_id)
_FK_DIM_COL_TABLES = [
    ("config_policies", "dimension"),
]


def upgrade() -> None:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    for old_id, new_id, new_cat, new_desc in _RENAMES:
        # ── 1. FK child tables first (safe regardless of FK enforcement) ─────
        for table in _FK_DIM_ID_TABLES:
            op.execute(f"""
                UPDATE {table}
                SET    dimension_id = '{new_id}'
                WHERE  dimension_id = '{old_id}'
            """)

        for table, col in _FK_DIM_COL_TABLES:
            op.execute(f"""
                UPDATE {table}
                SET    {col} = '{new_id}'
                WHERE  {col} = '{old_id}'
            """)

        # ── 2. Primary catalog table (PK rename + category + description) ────
        op.execute(f"""
            UPDATE config_evaluation_dimensions
            SET    dimension_id = '{new_id}',
                   category     = '{new_cat}',
                   description  = '{new_desc}',
                   updated_at   = '{now}'
            WHERE  dimension_id = '{old_id}'
        """)


def downgrade() -> None:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    # Reverse: new_id → old_id.  Category/description also restored.
    _REVERSE = [
        (new_id, old_id) for (old_id, new_id, _, __) in _RENAMES
    ]

    _OLD_META = {
        "sensitive_data_disclosure": ("security",  "Detects PII or sensitive credentials in content"),
        "data_minimisation_violation": ("privacy", "Detects exposure of personal data in AI responses"),
    }

    for new_id, old_id in _REVERSE:
        for table in _FK_DIM_ID_TABLES:
            op.execute(f"""
                UPDATE {table}
                SET    dimension_id = '{old_id}'
                WHERE  dimension_id = '{new_id}'
            """)

        for table, col in _FK_DIM_COL_TABLES:
            op.execute(f"""
                UPDATE {table}
                SET    {col} = '{old_id}'
                WHERE  {col} = '{new_id}'
            """)

        old_cat, old_desc = _OLD_META[new_id]
        op.execute(f"""
            UPDATE config_evaluation_dimensions
            SET    dimension_id = '{old_id}',
                   category     = '{old_cat}',
                   description  = '{old_desc}',
                   updated_at   = '{now}'
            WHERE  dimension_id = '{new_id}'
        """)
