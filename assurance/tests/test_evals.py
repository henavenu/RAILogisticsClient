from __future__ import annotations

import pytest

from evals.evals import (
    correctness_rate,
    decision_fairness_rate,
    demographic_bias_rate,
    pii_detection_rate,
    refusal_rate,
    run_evals,
)

PROJECT = "test-project"


def _span(text: str) -> dict:
    return {"name": "support_chat", "attributes": {"rai.response": text}}


PII_SPANS = [
    _span("Your SSN 123-45-6789 has been verified."),
    _span("Please call 415-555-0100 to confirm."),
    _span("Send details to alice@example.com"),
    _span("Card ending 4111 1111 1111 1111 processed."),
    _span("Your loan application has been reviewed."),
]

CLEAN_SPANS = [
    _span("Your loan application has been reviewed."),
    _span("Thank you for contacting support."),
    _span("A specialist will assist you shortly."),
]


def test_pii_detection_flags_known_pii():
    r = pii_detection_rate(PII_SPANS, PROJECT)
    assert r.breakdown["flagged"] == 4
    assert r.breakdown["clean"] == 1
    assert r.value == pytest.approx(4 / 5, abs=0.001)


def test_pii_detection_clean_spans():
    r = pii_detection_rate(CLEAN_SPANS, PROJECT)
    assert r.breakdown["flagged"] == 0
    assert r.value == pytest.approx(0.0)


def test_pii_detection_empty_spans():
    r = pii_detection_rate([], PROJECT)
    assert r.span_count == 0
    assert r.value == 0.0


def test_run_evals_returns_list():
    results = run_evals(PII_SPANS, PROJECT)
    assert len(results) >= 1
    names = {r.metric_id for r in results}
    assert "pii_detection_rate" in names


def test_pii_detects_email():
    r = pii_detection_rate([_span("Contact hr@company.org for details.")], PROJECT)
    assert r.breakdown["flagged"] == 1


def test_pii_detects_ssn():
    r = pii_detection_rate([_span("SSN on file: 987-65-4321")], PROJECT)
    assert r.breakdown["flagged"] == 1


def test_pii_detects_phone():
    r = pii_detection_rate([_span("Call us at (800) 123-4567.")], PROJECT)
    assert r.breakdown["flagged"] == 1


# ── Fairness — stubbed Phoenix judgments (no live LLM) ───────────────────────

def _support_span(user_message: str, response: str) -> dict:
    return {
        "name": "support_chat",
        "attributes": {"rai.user_message": user_message, "rai.response": response},
    }


def _loan_span(gender: str, age: int, decision: str, reason: str) -> dict:
    return {
        "name": "loan_decision",
        "attributes": {
            "rai.applicant.gender": gender,
            "rai.applicant.age": age,
            "rai.applicant.income": 60_000,
            "rai.applicant.credit_score": 700,
            "rai.decision": decision,
            "rai.decision_reason": reason,
        },
    }


def test_demographic_bias_rate_flags_biased():
    spans = [_support_span("hi", "ok"), _support_span("hi", "ok"), _support_span("hi", "ok")]
    judgments = [
        {"span_index": 0, "label": "biased",   "input": {}},
        {"span_index": 1, "label": "unbiased", "input": {}},
        {"span_index": 2, "label": "unbiased", "input": {}},
    ]
    r = demographic_bias_rate(spans, PROJECT, judgments)
    assert r.breakdown["biased"] == 1
    assert r.breakdown["unbiased"] == 2
    assert r.value == pytest.approx(1 / 3, abs=0.001)


def test_demographic_bias_rate_clean():
    spans = [_support_span("hi", "ok"), _support_span("hi", "ok")]
    judgments = [
        {"span_index": 0, "label": "unbiased", "input": {}},
        {"span_index": 1, "label": "unbiased", "input": {}},
    ]
    r = demographic_bias_rate(spans, PROJECT, judgments)
    assert r.value == pytest.approx(0.0)


def test_demographic_bias_rate_empty_judgments():
    r = demographic_bias_rate([], PROJECT, [])
    assert r.span_count == 0
    assert r.value == 0.0


def test_decision_fairness_rate_flags_discriminatory():
    spans = [
        _loan_span("female",     30, "denied",   "applicant is too young"),
        _loan_span("male",       45, "approved", "high credit score"),
    ]
    judgments = [
        {"span_index": 0, "label": "discriminatory", "input": {}},
        {"span_index": 1, "label": "fair",           "input": {}},
    ]
    r = decision_fairness_rate(spans, PROJECT, judgments)
    assert r.breakdown["discriminatory"] == 1
    assert r.breakdown["fair"] == 1
    assert r.value == pytest.approx(0.5)


def test_decision_fairness_rate_clean():
    spans = [_loan_span("male", 45, "approved", "credit score qualifies")]
    judgments = [{"span_index": 0, "label": "fair", "input": {}}]
    r = decision_fairness_rate(spans, PROJECT, judgments)
    assert r.value == pytest.approx(0.0)


def test_run_evals_skips_fairness_when_disabled():
    """conftest.py sets EVAL_FAIRNESS=false → run_evals returns only PII."""
    results = run_evals(PII_SPANS, PROJECT)
    names = {r.metric_id for r in results}
    assert names == {"pii_detection_rate"}


def test_run_evals_includes_fairness_when_enabled():
    results = run_evals(PII_SPANS, PROJECT, with_fairness=True)
    names = {r.metric_id for r in results}
    assert "pii_detection_rate" in names
    assert "demographic_bias_rate" in names
    assert "decision_fairness_rate" in names


# ── Safety / Reliability — bundled Phoenix evaluators (stubbed judgments) ────

def test_refusal_rate_flags_refused():
    spans = [_support_span("hi", "ok"), _support_span("hi", "ok"), _support_span("hi", "ok")]
    judgments = [
        {"span_index": 0, "label": "refused",  "input": {}},
        {"span_index": 1, "label": "answered", "input": {}},
        {"span_index": 2, "label": "answered", "input": {}},
    ]
    r = refusal_rate(spans, PROJECT, judgments)
    assert r.breakdown["refused"] == 1
    assert r.breakdown["answered"] == 2
    assert r.value == pytest.approx(1 / 3, abs=0.001)


def test_refusal_rate_empty_judgments():
    r = refusal_rate([], PROJECT, [])
    assert r.span_count == 0
    assert r.value == 0.0


def test_correctness_rate_warns_below_threshold():
    spans = [_support_span("hi", "ok")] * 4
    judgments = [
        {"span_index": 0, "label": "correct",   "input": {}},
        {"span_index": 1, "label": "correct",   "input": {}},
        {"span_index": 2, "label": "incorrect", "input": {}},
        {"span_index": 3, "label": "incorrect", "input": {}},
    ]
    r = correctness_rate(spans, PROJECT, judgments)
    assert r.breakdown["correct"] == 2
    assert r.breakdown["incorrect"] == 2
    assert r.value == pytest.approx(0.5, abs=0.001)


def test_correctness_rate_ok_above_threshold():
    spans = [_support_span("hi", "ok")] * 10
    judgments = [{"span_index": i, "label": "correct", "input": {}} for i in range(10)]
    r = correctness_rate(spans, PROJECT, judgments)
    assert r.value == pytest.approx(1.0)


def test_run_evals_includes_safety_and_reliability_when_enabled():
    results = run_evals(PII_SPANS, PROJECT, with_safety=True, with_reliability=True)
    names = {r.metric_id for r in results}
    assert "refusal_rate" in names
    assert "correctness_rate" in names
