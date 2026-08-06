"""Activate 6 previously-deferred evaluation dimensions

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-15

Activates the following dimensions so they contribute to MRA verdict synthesis
and the application trust score:

  SECURITY (Group A — zero code changes, provider already operational)
  ────────────────────────────────────────────────────────────────────
  prompt_injection  — TransferSecurityProvider (3-layer in-process detection)
  jailbreak         — TransferSecurityProvider (3-layer in-process detection)

  These two were deferred while TransferSecurityProvider was being built.
  The provider is now fully operational and registered in all modes including
  MOCK_MODE (it is in _LOCAL_PROVIDERS and is never overridden by mock_provider).

  PRIVACY / COMPLIANCE / FAIRNESS / TRANSPARENCY (Group B — mock keyword expansion)
  ─────────────────────────────────────────────────────────────────────────────────
  pii_exposure_flag            — mock_provider (keywords added in this release)
  eu_ai_act_transparency       — mock_provider (keywords expanded)
  eu_ai_act_prohibited_practice— mock_provider (keywords expanded)
  eu_ai_act_bias_discrimination — mock_provider (keywords expanded)

  NOTE: groundedness (transparency) remains deferred — evaluation delegated
  to Tier 2 (Arize assurance platform) where LLM-as-judge is appropriate.

Scoring impact
──────────────
  Before: deferred dims are excluded from MRA Rule 5 verdict synthesis and
          from the application trust score composite.
  After:  all 6 dims enter the verdict pipeline. A successful prompt injection
          or jailbreak attempt now produces a COERCIVE verdict and triggers the
          trust score COERCIVE penalty (−3 pp per event, capped at −35 pp).

Trust score weight categories affected
───────────────────────────────────────
  security:    prompt_injection, jailbreak now included
  privacy:     pii_exposure_flag now included
  compliance:  eu_ai_act_prohibited_practice now included
  fairness:    eu_ai_act_bias_discrimination now included (new category in score)
  transparency: eu_ai_act_transparency now included (new category in score)

  Because the trust score uses no-data rebalance (missing categories excluded
  from the composite denominator), apps with no traffic on new categories
  see no score change.  The new dims become meaningful only once evaluated
  content starts flowing through them.
"""
from datetime import datetime

from alembic import op


revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_ACTIVATE = [
    "prompt_injection",
    "jailbreak",
    "pii_exposure_flag",
    "eu_ai_act_transparency",
    "eu_ai_act_prohibited_practice",
    "eu_ai_act_bias_discrimination",
]


def upgrade() -> None:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    ids = ", ".join(f"'{d}'" for d in _ACTIVATE)
    op.execute(f"""
        UPDATE config_evaluation_dimensions
        SET    status     = 'active',
               updated_at = '{now}'
        WHERE  dimension_id IN ({ids})
          AND  status = 'deferred'
    """)


def downgrade() -> None:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    ids = ", ".join(f"'{d}'" for d in _ACTIVATE)
    op.execute(f"""
        UPDATE config_evaluation_dimensions
        SET    status     = 'deferred',
               updated_at = '{now}'
        WHERE  dimension_id IN ({ids})
    """)
