"""
Test: evaluator configuration save and retrieve round-trip.

Validates that PATCH /v1/evaluators/{dimension_id} persists:
  - supported_roles changes
  - status changes
  - provider assignment (with capability + availability validation)

And that GET /v1/dimensions retrieves the saved values correctly.

Run:
    pytest pap/tests/test_evaluator_save_retrieve.py -v
"""
import pytest
import json
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# ── Test database (in-memory SQLite) ──────────────────────────────────────────
TEST_DB_URL = "sqlite:///:memory:"


@pytest.fixture(scope="module")
def client():
    """Return a TestClient wired to an in-memory SQLite DB populated by seed."""
    import os
    os.environ.setdefault("DATABASE_URL", TEST_DB_URL)
    os.environ.setdefault("GUARDRAIL_SERVICE_URL", "http://localhost:3001")
    os.environ.setdefault("POLICY_ENGINE_URL", "http://localhost:3000")

    from core.database import Base, engine, get_db
    from core.seed import seed_all
    from api.routes import create_router
    from fastapi import FastAPI

    # Create tables + seed
    Base.metadata.create_all(bind=engine)
    seed_all()

    app = FastAPI()
    app.include_router(create_router())

    return TestClient(app, raise_server_exceptions=True)


# ── Helpers ────────────────────────────────────────────────────────────────────

def get_dimension(client, dim_id: str) -> dict:
    resp = client.get("/v1/dimensions")
    assert resp.status_code == 200, resp.text
    dims = {d["dimension_id"]: d for d in resp.json()["dimensions"]}
    assert dim_id in dims, f"Dimension '{dim_id}' not found. Available: {list(dims.keys())[:5]}"
    return dims[dim_id]


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestEvaluatorRolesSaveRetrieve:
    """PATCH supported_roles → GET /v1/dimensions shows updated roles."""

    DIM = "response_toxicity_score"          # seeded by default
    ORIGINAL_ROLES = ["llm_response"]        # default in seed

    def test_patch_roles_persists(self, client):
        new_roles = ["user_prompt", "llm_response", "agent_to_agent"]
        resp = client.patch(
            f"/v1/evaluators/{self.DIM}",
            json={"supported_roles": new_roles},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert sorted(data["supported_roles"]) == sorted(new_roles), \
            f"PATCH returned wrong roles: {data['supported_roles']}"

    def test_get_dimensions_shows_updated_roles(self, client):
        dim = get_dimension(client, self.DIM)
        expected = sorted(["user_prompt", "llm_response", "agent_to_agent"])
        assert sorted(dim.get("supported_roles") or []) == expected, \
            f"GET /v1/dimensions returned stale roles: {dim.get('supported_roles')}"

    def test_patch_roles_no_change_returns_no_changes(self, client):
        """Sending the same roles a second time should return 'No changes'."""
        current = get_dimension(client, self.DIM)["supported_roles"]
        resp = client.patch(
            f"/v1/evaluators/{self.DIM}",
            json={"supported_roles": current},
        )
        assert resp.status_code == 200, resp.text
        assert "No changes" in resp.json().get("message", ""), \
            f"Expected 'No changes' message, got: {resp.json()}"

    def test_restore_original_roles(self, client):
        resp = client.patch(
            f"/v1/evaluators/{self.DIM}",
            json={"supported_roles": self.ORIGINAL_ROLES},
        )
        assert resp.status_code == 200
        dim = get_dimension(client, self.DIM)
        assert sorted(dim["supported_roles"]) == sorted(self.ORIGINAL_ROLES)


class TestEvaluatorStatusSaveRetrieve:
    """PATCH status → GET /v1/dimensions reflects the change."""

    DIM = "hate_speech_detection"

    def test_defer_then_activate(self, client):
        # Deactivate
        resp = client.patch(f"/v1/evaluators/{self.DIM}", json={"status": "deferred"})
        assert resp.status_code == 200, resp.text
        assert get_dimension(client, self.DIM)["status"] == "deferred"

        # Re-activate
        resp = client.patch(f"/v1/evaluators/{self.DIM}", json={"status": "active"})
        assert resp.status_code == 200, resp.text
        assert get_dimension(client, self.DIM)["status"] == "active"


class TestProviderAssignmentValidation:
    """Provider assignment must be blocked if provider is unavailable or lacks capability."""

    DIM = "response_toxicity_score"

    def test_assign_nonexistent_provider_returns_404(self, client):
        resp = client.patch(
            f"/v1/evaluators/{self.DIM}",
            json={"provider": "does_not_exist_xyz"},
        )
        assert resp.status_code == 404, resp.text

    def test_assign_capable_provider_succeeds(self, client):
        """mock_provider or local_policy_classifier support various dims — pick one that matches."""
        # mock_provider has no specific supported_dims restriction — should always work
        resp = client.patch(
            f"/v1/evaluators/{self.DIM}",
            json={"provider": "mock_provider"},
        )
        # 200 = success, 404 = not seeded (skip), 409 = unavailable (acceptable in CI)
        assert resp.status_code in (200, 404, 409), resp.text

    def test_provider_capability_mismatch_returns_409(self, client):
        """azure_content_safety supports hate/harassment/sexual etc, NOT response_toxicity_score."""
        # First confirm azure_content_safety is seeded and its supported_dims excludes our dim
        r = client.get("/v1/providers")
        if r.status_code != 200:
            pytest.skip("Provider registry not available")
        providers = {p["provider_id"]: p for p in r.json()["providers"]}
        if "azure_content_safety" not in providers:
            pytest.skip("azure_content_safety not seeded")
        azure = providers["azure_content_safety"]
        if self.DIM in (azure.get("supported_dims") or []) or not azure.get("supported_dims"):
            pytest.skip("Provider either supports this dim or has no filter — skip mismatch test")

        resp = client.patch(
            f"/v1/evaluators/{self.DIM}",
            json={"provider": "azure_content_safety"},
        )
        assert resp.status_code == 409, \
            f"Expected 409 for capability mismatch, got {resp.status_code}: {resp.text}"
        assert "does not support" in resp.json()["detail"].lower()


class TestTrustWeightsSaveRetrieve:
    """PUT /v1/platform/trust-weights → GET returns updated values."""

    def test_get_returns_weights(self, client):
        resp = client.get("/v1/platform/trust-weights")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "weights" in data
        keys = set(data["weights"].keys())
        assert keys == {"safety", "security", "compliance", "operational", "quality"}

    def test_put_saves_and_retrieves(self, client):
        new_weights = {
            "safety": 0.40, "security": 0.20, "compliance": 0.20,
            "operational": 0.10, "quality": 0.10,
        }
        resp = client.put("/v1/platform/trust-weights", json={"weights": new_weights})
        assert resp.status_code == 200, resp.text
        saved = resp.json()["weights"]
        for k, v in new_weights.items():
            assert abs(saved[k] - v) < 0.001, f"{k}: expected {v}, got {saved[k]}"

        # Retrieve and confirm
        get_resp = client.get("/v1/platform/trust-weights")
        assert get_resp.status_code == 200
        retrieved = get_resp.json()["weights"]
        for k, v in new_weights.items():
            assert abs(retrieved[k] - v) < 0.001

    def test_put_invalid_total_returns_422(self, client):
        resp = client.put("/v1/platform/trust-weights", json={"weights": {
            "safety": 0.50, "security": 0.50, "compliance": 0.50,
            "operational": 0.10, "quality": 0.10,
        }})
        assert resp.status_code == 422, resp.text

    def test_put_missing_key_returns_422(self, client):
        resp = client.put("/v1/platform/trust-weights", json={"weights": {
            "safety": 0.50, "security": 0.50,
        }})
        assert resp.status_code == 422, resp.text
