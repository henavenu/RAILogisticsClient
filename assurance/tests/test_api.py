from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "timestamp" in body


def test_jobs_run_returns_expected_shape():
    r = client.post("/jobs/run")
    assert r.status_code == 200
    body = r.json()
    assert body["metrics_computed"] == 13
    assert "completed_at" in body


def test_metrics_list_returns_all():
    client.post("/jobs/run")
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 13
    ids = {m["metric_id"] for m in body["metrics"]}
    assert "approval_rate_disparity_by_gender" in ids
    assert "toxicity_rate" in ids
    assert "decisioning_latency_p50_ms" in ids
    assert "pii_detection_rate" in ids


def test_metric_by_name_found():
    client.post("/jobs/run")
    r = client.get("/metrics/toxicity_rate")
    assert r.status_code == 200
    body = r.json()
    assert body["metric_id"] == "toxicity_rate"
    assert "value" in body


def test_metric_by_name_not_found():
    r = client.get("/metrics/nonexistent_metric")
    assert r.status_code == 404


def test_metrics_summary_shape():
    client.post("/jobs/run")
    r = client.get("/metrics/summary")
    assert r.status_code == 200
    body = r.json()
    assert "pillars" in body
    assert set(body["pillars"].keys()) == {"fairness", "safety", "reliability", "privacy"}
    assert "total_metrics" in body


def test_pillar_metric_counts():
    client.post("/jobs/run")
    r = client.get("/metrics/summary")
    pillars = r.json()["pillars"]
    assert pillars["fairness"]["metric_count"] == 4
    assert pillars["safety"]["metric_count"] == 4
    assert pillars["reliability"]["metric_count"] == 4
    assert pillars["privacy"]["metric_count"] == 1


def test_privacy_pillar_contains_pii():
    client.post("/jobs/run")
    r = client.get("/metrics/summary")
    privacy = r.json()["pillars"]["privacy"]
    assert "pii_detection_rate" in privacy["metrics"]
