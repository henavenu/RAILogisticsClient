from __future__ import annotations

from core.agents.base import BiasDetectionProvider, BiasReport
from core.agents.mock_provider import MockBiasProvider
from core.agents.registry import bias_agents
from core.agents.span_adapter import spans_to_records


def test_mock_provider_implements_spi():
    provider = MockBiasProvider()
    assert isinstance(provider, BiasDetectionProvider)
    assert provider.is_available() is True


def test_mock_provider_returns_bias_report():
    provider = MockBiasProvider()
    records = [{"id": "1", "gender": "Male", "decision": "Approved"}]
    report = provider.detect(records)
    assert isinstance(report, BiasReport)
    assert report.provider == "mock"
    assert report.records_analyzed == 1
    assert len(report.metrics) >= 1
    assert report.risk_level in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}


def test_registry_lists_providers():
    available = bias_agents.available()
    assert "mock" in available


def test_registry_active_uses_env(monkeypatch):
    monkeypatch.setenv("BIAS_AGENT_PROVIDER", "mock")
    active = bias_agents.get_active()
    assert active.name == "mock"


def test_registry_unknown_provider_raises():
    import pytest
    with pytest.raises(ValueError):
        bias_agents.get("does-not-exist")


def test_span_adapter_filters_to_loan_decision_spans():
    spans = [
        {"name": "loan_decision", "attributes": {"rai.gender": "Female", "rai.age": 32, "rai.decision": "approve"}},
        {"name": "support_chat",  "attributes": {"rai.user_message": "hi"}},
        {"name": "loan_decision", "attributes": {"rai.gender": "Male", "rai.age": 45, "rai.decision": "deny"}},
    ]
    records = spans_to_records(spans)
    assert len(records) == 2
    assert records[0] == {"id": "S0", "decision": "Approved", "gender": "Female", "age": 32}
    assert records[1]["decision"] == "Denied"


def test_span_adapter_drops_spans_without_decision():
    spans = [
        {"name": "loan_decision", "attributes": {"rai.gender": "Female"}},
        {"name": "loan_decision", "attributes": {"rai.gender": "Male", "rai.decision": "approve"}},
    ]
    records = spans_to_records(spans)
    assert len(records) == 1
    assert records[0]["gender"] == "Male"


def test_span_adapter_handles_missing_age_gracefully():
    spans = [
        {"name": "loan_decision", "attributes": {"rai.gender": "Female", "rai.age": "", "rai.decision": "approve"}},
    ]
    records = spans_to_records(spans)
    assert len(records) == 1
    assert "age" not in records[0]
