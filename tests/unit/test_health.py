"""``/health`` and ``/ready`` endpoint tests.

Uses FastAPI's ``TestClient`` to drive the routes synchronously. The
backend is the test-only :class:`NoopBackend` — no socket, no httpx,
no race conditions.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from llm_gateway.app import create_app
from llm_gateway.config import Settings
from llm_gateway.inference.noop import NoopBackend


def _client(*, reachable: bool) -> TestClient:
    settings = Settings()
    app = create_app(settings=settings, backend=NoopBackend(reachable=reachable))
    return TestClient(app)


# ─── /health ────────────────────────────────────────────────────────────────


def test_health_returns_200_with_ok_status() -> None:
    client = _client(reachable=True)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_is_unaffected_by_backend_state() -> None:
    """Liveness MUST stay green even when readiness flips — that's the
    whole point of the K8s-style probe split."""
    client = _client(reachable=False)
    assert client.get("/health").status_code == 200


# ─── /ready ─────────────────────────────────────────────────────────────────


def test_ready_returns_200_when_backend_reachable() -> None:
    client = _client(reachable=True)
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ready_returns_503_when_backend_unreachable() -> None:
    client = _client(reachable=False)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unready"}
