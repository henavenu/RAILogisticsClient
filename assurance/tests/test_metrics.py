from __future__ import annotations

import pytest

from metrics.tier2metrics import (
    approval_rate_disparity_by_gender,
    denial_rate_disparity_by_gender,
    risk_score_disparity_by_gender,
    approval_rate_disparity_by_age,
    toxicity_rate,
    offensive_content_rate,
    escalation_rate,
    negative_sentiment_rate,
    decisioning_latency_p50_ms,
    conversational_latency_p50_ms,
    decisioning_score_consistency,
    conversational_safe_interaction_rate,
)

PROJECT = "test-project"

# ── Shared fixtures ───────────────────────────────────────────────────────────

def _loan(gender, decision, risk_score, age, latency_ms=2000):
    ns = latency_ms * 1_000_000
    return {
        "name": "loan_decision",
        "start_time": 0,
        "end_time": ns,
        "attributes": {
            "rai.applicant.gender": gender,
            "rai.applicant.age": age,
            "rai.decision": decision,
            "rai.risk_score": risk_score,
        },
    }


def _support(intent, sentiment, toxicity_flagged, safety_category, latency_ms=1000):
    ns = latency_ms * 1_000_000
    return {
        "name": "support_chat",
        "start_time": 0,
        "end_time": ns,
        "attributes": {
            "rai.intent": intent,
            "rai.sentiment": sentiment,
            "rai.toxicity_flagged": toxicity_flagged,
            "rai.safety_category": safety_category,
        },
    }


LOAN_SPANS = [
    _loan("female",     "approved", 25, 32),
    _loan("female",     "approved", 30, 28),
    _loan("male",       "approved", 20, 45),
    _loan("male",       "approved", 22, 55),
    _loan("non-binary", "denied",   80, 23),
]

SUPPORT_SPANS = [
    _support("billing",    "negative", False, "safe"),
    _support("technical",  "negative", False, "safe"),
    _support("complaint",  "negative", True,  "offensive"),
    _support("general",    "neutral",  False, "safe"),
    _support("escalation", "negative", False, "safe"),
    _support("general",    "neutral",  False, "safe"),
]


# ── Fairness ──────────────────────────────────────────────────────────────────

def test_approval_rate_disparity_detects_gap():
    r = approval_rate_disparity_by_gender(LOAN_SPANS, PROJECT)
    # female=1.0, male=1.0, non-binary=0.0 → disparity=1.0
    assert r.breakdown["non-binary"] == 0.0
    assert r.breakdown["female"] == 1.0
    assert r.value == pytest.approx(1.0)
    assert r.metric_id == "approval_rate_disparity_by_gender"


def test_approval_rate_disparity_ok_when_equal():
    spans = [
        _loan("female", "approved", 30, 30),
        _loan("male",   "approved", 30, 30),
    ]
    r = approval_rate_disparity_by_gender(spans, PROJECT)
    assert r.value == pytest.approx(0.0)


def test_denial_rate_disparity():
    r = denial_rate_disparity_by_gender(LOAN_SPANS, PROJECT)
    assert r.breakdown["non-binary"] == 1.0
    assert r.breakdown["female"] == 0.0


def test_risk_score_disparity_by_gender():
    r = risk_score_disparity_by_gender(LOAN_SPANS, PROJECT)
    assert r.breakdown["non-binary"] == pytest.approx(80.0)


def test_approval_rate_disparity_by_age():
    r = approval_rate_disparity_by_age(LOAN_SPANS, PROJECT)
    assert r.breakdown["18-30"] == pytest.approx(0.5)
    assert r.span_count == len(LOAN_SPANS)


# ── Safety ────────────────────────────────────────────────────────────────────

def test_toxicity_rate():
    r = toxicity_rate(SUPPORT_SPANS, PROJECT)
    assert r.value == pytest.approx(1 / 6, abs=0.001)


def test_toxicity_rate_empty_spans():
    r = toxicity_rate([], PROJECT)
    assert r.span_count == 0
    assert r.value == 0.0


def test_offensive_content_rate():
    r = offensive_content_rate(SUPPORT_SPANS, PROJECT)
    assert r.value == pytest.approx(1 / 6, abs=0.001)


def test_escalation_rate():
    r = escalation_rate(SUPPORT_SPANS, PROJECT)
    assert r.value == pytest.approx(1 / 6, abs=0.001)


def test_negative_sentiment_rate():
    r = negative_sentiment_rate(SUPPORT_SPANS, PROJECT)
    assert r.value == pytest.approx(4 / 6, abs=0.001)


# ── Reliability ───────────────────────────────────────────────────────────────

def test_decisioning_latency_p50():
    r = decisioning_latency_p50_ms(LOAN_SPANS, PROJECT)
    assert r.value == pytest.approx(2000.0)


def test_conversational_latency_p50():
    r = conversational_latency_p50_ms(SUPPORT_SPANS, PROJECT)
    assert r.value == pytest.approx(1000.0)


def test_decisioning_score_consistency():
    r = decisioning_score_consistency(LOAN_SPANS, PROJECT)
    assert r.value > 20


def test_decisioning_score_consistency_needs_two_spans():
    r = decisioning_score_consistency([LOAN_SPANS[0]], PROJECT)
    assert r.span_count == 0   # single span → no_data fallback


def test_conversational_safe_interaction_rate():
    r = conversational_safe_interaction_rate(SUPPORT_SPANS, PROJECT)
    assert r.value == pytest.approx(5 / 6, abs=0.001)
