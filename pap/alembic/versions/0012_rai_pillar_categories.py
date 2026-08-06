"""Remap evaluation dimensions to RAI pillar taxonomy

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-07

Aligns config_evaluation_dimensions.category with the industry-standard
Responsible AI pillar taxonomy:

  Safety       — content harm (unchanged)
  Security     — adversarial attacks (removes data_leakage / pii_exposure_flag)
  Privacy      — PII / data leakage  (NEW — was split across security)
  Compliance   — regulatory obligations (replaces financial_crime + eu_ai_act
                 prohibited practice)
  Fairness     — bias / discrimination (NEW — was eu_ai_act)
  Transparency — AI disclosure / explainability (NEW — was eu_ai_act + trust)

Operational categories kept but excluded from T1 dashboard display:
  corporate_governance · prompt_filter · session_trackers

Industry rationale
------------------
  data_leakage / pii_exposure_flag → Privacy
    OWASP LLM Top 10 LLM06 (Sensitive Information Disclosure),
    GDPR Art. 5 data minimisation, NIST AI RMF Privacy category.
    Enforcement action on a leaked response is a privacy incident,
    not a security attack (that's what prompt_injection covers).

  financial_crime dimensions → Compliance
    FATF recommendations, OFAC sanctions programme, FinCEN AML rules.
    These are regulatory compliance obligations — treating them as a
    standalone "financial crime" category has no industry-standard
    AI pillar equivalent.  Compliance is the correct home.

  eu_ai_act_prohibited_practice → Compliance
    EU AI Act Art. 5 prohibited practices (subliminal manipulation etc.)
    are regulatory compliance obligations, not purely an ethics pillar.

  eu_ai_act_bias_discrimination → Fairness
    EU AI Act Art. 10 / NIST AI RMF Bias category.

  eu_ai_act_transparency + groundedness → Transparency
    EU AI Act Art. 13 AI disclosure obligation.
    groundedness (model quality / factual accuracy) is the primary
    real-world signal for transparency / explainability.
"""
from datetime import datetime

from alembic import op


revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    # ── 1. Privacy: move data_leakage + pii_exposure_flag out of security ────
    op.execute(f"""
        UPDATE config_evaluation_dimensions
        SET    category   = 'privacy',
               updated_at = '{now}'
        WHERE  dimension_id IN ('data_leakage', 'pii_exposure_flag')
    """)

    # ── 2. Compliance: rename financial_crime dimensions ─────────────────────
    op.execute(f"""
        UPDATE config_evaluation_dimensions
        SET    category   = 'compliance',
               updated_at = '{now}'
        WHERE  dimension_id IN (
            'sanctions_country_reference',
            'sanctions_entity_mention',
            'sanctions_evasion_signal',
            'aml_structuring_signal'
        )
    """)

    # ── 3. Compliance: move eu_ai_act_prohibited_practice ────────────────────
    op.execute(f"""
        UPDATE config_evaluation_dimensions
        SET    category   = 'compliance',
               updated_at = '{now}'
        WHERE  dimension_id = 'eu_ai_act_prohibited_practice'
    """)

    # ── 4. Fairness: move eu_ai_act_bias_discrimination ──────────────────────
    op.execute(f"""
        UPDATE config_evaluation_dimensions
        SET    category   = 'fairness',
               updated_at = '{now}'
        WHERE  dimension_id = 'eu_ai_act_bias_discrimination'
    """)

    # ── 5. Transparency: move eu_ai_act_transparency + groundedness ──────────
    op.execute(f"""
        UPDATE config_evaluation_dimensions
        SET    category   = 'transparency',
               updated_at = '{now}'
        WHERE  dimension_id IN ('eu_ai_act_transparency', 'groundedness')
    """)


def downgrade() -> None:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    # Restore security
    op.execute(f"""
        UPDATE config_evaluation_dimensions
        SET    category   = 'security',
               updated_at = '{now}'
        WHERE  dimension_id IN ('data_leakage', 'pii_exposure_flag')
    """)

    # Restore financial_crime
    op.execute(f"""
        UPDATE config_evaluation_dimensions
        SET    category   = 'financial_crime',
               updated_at = '{now}'
        WHERE  dimension_id IN (
            'sanctions_country_reference',
            'sanctions_entity_mention',
            'sanctions_evasion_signal',
            'aml_structuring_signal'
        )
    """)

    # Restore eu_ai_act
    op.execute(f"""
        UPDATE config_evaluation_dimensions
        SET    category   = 'eu_ai_act',
               updated_at = '{now}'
        WHERE  dimension_id IN (
            'eu_ai_act_prohibited_practice',
            'eu_ai_act_bias_discrimination',
            'eu_ai_act_transparency'
        )
    """)

    # Restore trust
    op.execute(f"""
        UPDATE config_evaluation_dimensions
        SET    category   = 'trust',
               updated_at = '{now}'
        WHERE  dimension_id = 'groundedness'
    """)
