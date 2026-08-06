"""Tests for the 6 SRS §4.7.3 mandatory metrics — exercises the aggregation logic
with stub judgments so no live Phoenix LLM calls are made."""
from __future__ import annotations

from evals.evals import (
    groundedness_score,
    hallucination_rate,
    pii_exposure_rate,
    prompt_injection_rate,
    response_relevance,
    toxicity_judge_rate,
)

PROJECT = "test-project"


def _stub(label: str, idx: int = 0) -> dict:
    return {"span_index": idx, "label": label, "input": {}, "explanation": None}


# ── Hallucination ────────────────────────────────────────────────────────────

def test_hallucination_rate_all_grounded():
    js = [_stub("grounded"), _stub("grounded", 1)]
    r = hallucination_rate([], PROJECT, judgments=js)
    assert r.value == 0.0
    assert r.span_count == 2
    assert r.metric_id == "hallucination_rate"


def test_hallucination_rate_with_flags():
    js = [_stub("hallucinated"), _stub("grounded", 1), _stub("hallucinated", 2)]
    r = hallucination_rate([], PROJECT, judgments=js)
    assert r.value == round(2 / 3, 3)
    assert r.breakdown == {"hallucinated": 2, "grounded": 1}


def test_hallucination_rate_no_judgments_returns_zero_span():
    r = hallucination_rate([], PROJECT, judgments=[])
    assert r.span_count == 0
    assert r.value == 0.0


# ── Groundedness (positive score — higher is better) ─────────────────────────

def test_groundedness_score_perfect_score():
    js = [_stub("grounded"), _stub("grounded", 1)]
    r = groundedness_score([], PROJECT, judgments=js)
    assert r.value == 1.0
    assert r.unit == "score"


def test_groundedness_score_below_threshold_warns():
    js = [_stub("grounded"), _stub("ungrounded", 1)]
    r = groundedness_score([], PROJECT, judgments=js)
    assert r.value == 0.5


# ── Response relevance (positive score) ──────────────────────────────────────

def test_response_relevance_all_relevant():
    js = [_stub("relevant"), _stub("relevant", 1), _stub("relevant", 2)]
    r = response_relevance([], PROJECT, judgments=js)
    assert r.value == 1.0
    assert r.unit == "score"


def test_response_relevance_some_irrelevant():
    js = [_stub("relevant"), _stub("irrelevant", 1)]
    r = response_relevance([], PROJECT, judgments=js)
    assert r.value == 0.5


# ── Toxicity (LLM-as-judge) ──────────────────────────────────────────────────

def test_toxicity_judge_rate_all_safe():
    js = [_stub("safe"), _stub("safe", 1)]
    r = toxicity_judge_rate([], PROJECT, judgments=js)
    assert r.value == 0.0


def test_toxicity_judge_rate_with_toxic():
    js = [_stub("toxic"), _stub("safe", 1)]
    r = toxicity_judge_rate([], PROJECT, judgments=js)
    assert r.value == 0.5
    assert r.breakdown == {"toxic": 1, "safe": 1}


# ── Prompt injection ─────────────────────────────────────────────────────────

def test_prompt_injection_rate_all_benign():
    js = [_stub("benign"), _stub("benign", 1)]
    r = prompt_injection_rate([], PROJECT, judgments=js)
    assert r.value == 0.0


def test_prompt_injection_rate_with_attempt():
    js = [_stub("injection"), _stub("benign", 1), _stub("benign", 2)]
    r = prompt_injection_rate([], PROJECT, judgments=js)
    assert r.value == round(1 / 3, 3)


# ── PII exposure (LLM-as-judge, complements regex) ───────────────────────────

def test_pii_exposure_rate_no_disclosure():
    js = [_stub("no_disclosure"), _stub("no_disclosure", 1)]
    r = pii_exposure_rate([], PROJECT, judgments=js)
    assert r.value == 0.0


def test_pii_exposure_rate_with_disclosure():
    js = [_stub("discloses_pii"), _stub("no_disclosure", 1)]
    r = pii_exposure_rate([], PROJECT, judgments=js)
    assert r.value == 0.5
    assert r.breakdown == {"discloses": 1, "clean": 1}


# ── /v1 catalog contains all 6 ───────────────────────────────────────────────

def test_v1_catalog_includes_srs_mandatory_metrics():
    from api.v1.router import _CATALOG

    for mid in [
        "hallucination_rate",
        "groundedness_score",
        "response_relevance",
        "toxicity_judge_rate",
        "prompt_injection_rate",
        "pii_exposure_rate",
    ]:
        assert mid in _CATALOG, f"{mid} missing from /v1 catalog"
