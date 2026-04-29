"""``Metrics`` registry + ``/metrics`` endpoint contract."""

from __future__ import annotations

from fastapi.testclient import TestClient
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from llm_gateway.app import create_app
from llm_gateway.config import Settings
from llm_gateway.inference.noop import NoopBackend
from llm_gateway.observability.metrics import Metrics

# ─── Metrics registry isolation ────────────────────────────────────────────


def test_each_metrics_instance_owns_an_independent_registry() -> None:
    a = Metrics()
    b = Metrics()
    a.requests_total.labels(method="GET", path="/x", status="200").inc()

    a_dump = generate_latest(a.registry).decode()
    b_dump = generate_latest(b.registry).decode()

    assert "llm_gateway_requests_total" in a_dump
    # b's same series exists but has no observations → metric line is
    # absent / value 0.0 (counters with no labels seen yet emit nothing).
    assert "llm_gateway_requests_total{" not in b_dump


def test_circuit_breaker_state_initialises_to_closed() -> None:
    metrics = Metrics()
    dump = generate_latest(metrics.registry).decode()
    assert "llm_gateway_circuit_breaker_state 0.0" in dump


# ─── /metrics endpoint contract ────────────────────────────────────────────


def _client(metrics: Metrics) -> TestClient:
    return TestClient(
        create_app(
            settings=Settings(bearer_token=""),
            backend=NoopBackend(),
            metrics=metrics,
        )
    )


def test_metrics_endpoint_returns_prometheus_content_type() -> None:
    response = _client(Metrics()).get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPE_LATEST


def test_metrics_endpoint_bypasses_auth() -> None:
    """Prometheus scrapers don't carry tokens; the SSH tunnel is the
    auth boundary."""
    settings = Settings(bearer_token="placeholder-bearer")  # pragma: allowlist secret
    metrics = Metrics()
    app = create_app(settings=settings, backend=NoopBackend(), metrics=metrics)
    response = TestClient(app).get("/metrics")
    assert response.status_code == 200
