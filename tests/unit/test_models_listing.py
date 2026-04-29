"""``GET /v1/models`` endpoint tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from llm_gateway.app import create_app
from llm_gateway.config import Settings
from llm_gateway.inference.noop import NoopBackend


def _client() -> TestClient:
    return TestClient(create_app(settings=Settings(bearer_token=""), backend=NoopBackend()))


def test_list_models_returns_openai_envelope_with_registry_entries() -> None:
    response = _client().get("/v1/models")

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    ids = [item["id"] for item in body["data"]]
    assert "selfhost-qwen" in ids


def test_list_models_each_entry_has_object_model_and_owner() -> None:
    response = _client().get("/v1/models")
    for item in response.json()["data"]:
        assert item["object"] == "model"
        assert item["owned_by"] == "self-hosted"
