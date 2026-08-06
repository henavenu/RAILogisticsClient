from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def test_v1_healthz_responds():
    r = client.get("/v1/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in {"ok", "degraded", "down"}
    assert body["provider_status"] in {"reachable", "unreachable", "unknown"}


def test_v1_catalog_returns_metric_list():
    r = client.get("/v1/catalog")
    assert r.status_code == 200
    body = r.json()
    assert body["metric_count"] > 0
    assert body["metric_count"] == len(body["metrics"])
    item = body["metrics"][0]
    assert "metric_id" in item
    assert item["unit"] in {"%", "score", "count", "ms", ""}


def test_v1_metrics_requires_app_id():
    r = client.get("/v1/metrics")
    assert r.status_code == 422


def test_v1_metrics_returns_envelope():
    r = client.get("/v1/metrics", params={"app_id": "test-app", "window": "1h"})
    assert r.status_code == 200
    body = r.json()
    assert body["app_id"] == "test-app"
    assert body["window"] == "1h"
    assert body["data_status"] in {"ok", "no_data", "provider_unavailable", "unsupported"}
    assert body["metric_count"] == len(body["metrics"])
    assert body["metric_count"] > 0


def test_v1_metrics_ids_filter_returns_subset():
    r = client.get(
        "/v1/metrics",
        params={"app_id": "test-app", "ids": "pii_detection_rate,toxicity_rate", "window": "1h"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["metric_count"] == 2
    ids = {m["metric_id"] for m in body["metrics"]}
    assert ids == {"pii_detection_rate", "toxicity_rate"}


def test_v1_metrics_zero_samples_yields_null_value():
    r = client.get(
        "/v1/metrics",
        params={"app_id": "test-app", "ids": "pii_detection_rate", "window": "1h"},
    )
    body = r.json()
    item = body["metrics"][0]
    if item["data_status"] == "no_data":
        assert item["value"] is None


def test_v1_metrics_unknown_id_returns_unsupported_not_400():
    r = client.get(
        "/v1/metrics",
        params={"app_id": "test-app", "ids": "does_not_exist", "window": "1h"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["metric_count"] == 1
    assert body["metrics"][0]["data_status"] == "unsupported"
    assert body["metrics"][0]["value"] is None


def test_v1_metrics_invalid_window_rejects():
    r = client.get("/v1/metrics", params={"app_id": "test-app", "window": "5m"})
    assert r.status_code == 400


def test_v1_metric_by_id_path_returns_single_item_envelope():
    r = client.get(
        "/v1/metrics/pii_detection_rate",
        params={"app_id": "test-app", "window": "1h"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["metric_count"] == 1
    assert body["metrics"][0]["metric_id"] == "pii_detection_rate"


def test_v1_openapi_spec_served():
    r = client.get("/v1/openapi.json")
    assert r.status_code == 200
    assert r.json()["info"]["title"] == "RAI Assurance Service"


def test_v1_docs_ui_served():
    r = client.get("/v1/docs")
    assert r.status_code == 200


# ── Gap 7 — since/until ISO range ────────────────────────────────────────────

def test_v1_metrics_accepts_since_until_range():
    r = client.get(
        "/v1/metrics",
        params={
            "app_id": "test-app",
            "since": "2026-05-01T00:00:00Z",
            "until": "2026-05-02T00:00:00Z",
        },
    )
    assert r.status_code == 200
    body = r.json()
    # Window field echoes the resolved range
    assert "__" in body["window"]


def test_v1_metrics_rejects_window_and_since_together():
    r = client.get(
        "/v1/metrics",
        params={"app_id": "test-app", "window": "1h", "since": "2026-05-01T00:00:00Z"},
    )
    assert r.status_code == 400


def test_v1_metrics_rejects_invalid_iso():
    r = client.get(
        "/v1/metrics",
        params={"app_id": "test-app", "since": "yesterday"},
    )
    assert r.status_code == 400


def test_v1_metrics_rejects_since_after_until():
    r = client.get(
        "/v1/metrics",
        params={
            "app_id": "test-app",
            "since": "2026-05-02T00:00:00Z",
            "until": "2026-05-01T00:00:00Z",
        },
    )
    assert r.status_code == 400


def test_v1_metrics_rejects_range_over_7_days():
    r = client.get(
        "/v1/metrics",
        params={
            "app_id": "test-app",
            "since": "2026-05-01T00:00:00Z",
            "until": "2026-05-10T00:00:00Z",
        },
    )
    assert r.status_code == 400


def test_v1_metrics_default_window_is_1h_when_nothing_supplied():
    r = client.get("/v1/metrics", params={"app_id": "test-app"})
    assert r.status_code == 200
    assert r.json()["window"] == "1h"


# ── /v1/llm-evals — Phoenix LLM-as-judge endpoint ────────────────────────────

def test_v1_llm_evals_returns_envelope():
    r = client.get("/v1/llm-evals", params={"app_id": "test-app", "window": "1h"})
    assert r.status_code == 200
    body = r.json()
    assert body["app_id"] == "test-app"
    assert body["window"] == "1h"
    assert body["data_status"] in {"ok", "no_data", "provider_unavailable", "unsupported"}
    # All 10 LLM-as-judge metrics returned by default
    assert body["metric_count"] == 10


def test_v1_llm_evals_includes_srs_mandatory_six():
    r = client.get("/v1/llm-evals", params={"app_id": "test-app", "window": "1h"})
    body = r.json()
    ids = {m["metric_id"] for m in body["metrics"]}
    for mid in [
        "hallucination_rate",
        "groundedness_score",
        "response_relevance",
        "toxicity_judge_rate",
        "prompt_injection_rate",
        "pii_exposure_rate",
    ]:
        assert mid in ids, f"{mid} missing from /v1/llm-evals response"


def test_v1_llm_evals_rejects_non_llm_metric():
    r = client.get(
        "/v1/llm-evals",
        params={"app_id": "test-app", "ids": "approval_rate_disparity_by_gender", "window": "1h"},
    )
    assert r.status_code == 400


def test_v1_llm_evals_ids_filter():
    r = client.get(
        "/v1/llm-evals",
        params={"app_id": "test-app", "ids": "hallucination_rate,toxicity_judge_rate", "window": "1h"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["metric_count"] == 2
