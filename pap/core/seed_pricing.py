"""
Seed config_model_pricing with current LLM pricing.

Prices are in USD per 1 million tokens — the industry standard unit.
Run idempotently: existing rows are never overwritten.

Sources (as of 2026-04):
  Groq   — https://console.groq.com/docs/openai-compatibility
  OpenAI — https://openai.com/api/pricing
  Anthropic — https://www.anthropic.com/pricing
  Google — https://ai.google.dev/pricing

Run:
    cd pap && python -m core.seed_pricing
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

# Allow running as a script from pap/ and load service .env so
# PAP_DATABASE_URL points to config/pap.db (not the rai_trust.db default).
_PAP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PAP_DIR))

from dotenv import load_dotenv
load_dotenv(_PAP_DIR / ".env")

from core.database import SessionLocal
from core.models import ModelPricing

_EFFECTIVE_FROM = datetime(2026, 4, 1)   # pricing snapshot date

# (provider, model_id, cost_per_1m_input, cost_per_1m_output)
_PRICING: list[tuple[str, str, float, float]] = [

    # ── Groq (ultra-low latency inference) ───────────────────────────────────
    ("groq", "llama-3.1-8b-instant",       0.05,   0.08),
    ("groq", "llama-3.1-70b-versatile",    0.59,   0.79),
    ("groq", "llama-3.3-70b-versatile",    0.59,   0.79),
    ("groq", "llama3-8b-8192",             0.05,   0.08),
    ("groq", "llama3-70b-8192",            0.59,   0.79),
    ("groq", "mixtral-8x7b-32768",         0.24,   0.24),
    ("groq", "gemma2-9b-it",               0.20,   0.20),

    # ── OpenAI ────────────────────────────────────────────────────────────────
    ("openai", "gpt-4o",                   2.50,  10.00),
    ("openai", "gpt-4o-mini",              0.15,   0.60),
    ("openai", "gpt-4-turbo",             10.00,  30.00),
    ("openai", "gpt-4",                   30.00,  60.00),
    ("openai", "gpt-3.5-turbo",            0.50,   1.50),
    ("openai", "o1",                      15.00,  60.00),
    ("openai", "o1-mini",                  3.00,  12.00),
    ("openai", "o3-mini",                  1.10,   4.40),

    # ── Anthropic ─────────────────────────────────────────────────────────────
    ("anthropic", "claude-3-5-sonnet-20241022",   3.00,  15.00),
    ("anthropic", "claude-3-5-haiku-20241022",    0.80,   4.00),
    ("anthropic", "claude-3-opus-20240229",       15.00,  75.00),
    ("anthropic", "claude-3-sonnet-20240229",      3.00,  15.00),
    ("anthropic", "claude-3-haiku-20240307",       0.25,   1.25),

    # ── Google ────────────────────────────────────────────────────────────────
    ("google", "gemini-1.5-pro",           1.25,   5.00),
    ("google", "gemini-1.5-flash",         0.075,  0.30),
    ("google", "gemini-2.0-flash",         0.10,   0.40),

    # ── AWS Bedrock (on-demand, us-east-1) ───────────────────────────────────
    ("aws",  "amazon.titan-text-express-v1",  0.20,  0.60),
    ("aws",  "anthropic.claude-3-sonnet",     3.00, 15.00),
    ("aws",  "meta.llama3-8b-instruct-v1",    0.22,  0.22),
    ("aws",  "meta.llama3-70b-instruct-v1",   0.99,  0.99),

    # ── Azure OpenAI (East US, PayG) ──────────────────────────────────────────
    ("azure", "gpt-4o",                    2.50,  10.00),
    ("azure", "gpt-4o-mini",               0.165,  0.66),
    ("azure", "gpt-4-turbo",              10.00,  30.00),
]


def seed_pricing(session) -> int:
    """Insert pricing rows that don't already exist. Returns count of new rows."""
    inserted = 0
    for provider, model_id, cost_in, cost_out in _PRICING:
        exists = session.query(ModelPricing).filter_by(
            provider=provider,
            model_id=model_id,
            effective_from=_EFFECTIVE_FROM,
        ).first()
        if exists:
            continue
        session.add(ModelPricing(
            provider=provider,
            model_id=model_id,
            cost_per_1m_input=cost_in,
            cost_per_1m_output=cost_out,
            effective_from=_EFFECTIVE_FROM,
            effective_until=None,    # NULL = currently active
            active=True,
        ))
        inserted += 1
    session.commit()
    return inserted


if __name__ == "__main__":
    db = SessionLocal()
    try:
        n = seed_pricing(db)
        total = db.query(ModelPricing).count()
        print(f"Seeded {n} new pricing rows  ({total} total in config_model_pricing)")
    finally:
        db.close()
