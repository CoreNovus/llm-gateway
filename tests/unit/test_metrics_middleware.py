"""``MetricsMiddleware`` + chat-completions token instrumentation tests."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from prometheus_client import generate_latest

from llm_gateway.app import create_app
from llm_gateway.config import Settings
from llm_gateway.inference.noop import NoopBackend
from llm_gateway.observability.metrics import Metrics


def _client(metrics: Metrics, backend: object | None = None) -> TestClient:
    return TestClient(
        create_app(
            settings=Settings(bearer_token=""),
            backend=backend or NoopBackend(),  # type: ignore[arg-type]
            metrics=metrics,
        )
    )


def _scrape(metrics: Metrics) -> str:
    return generate_latest(metrics.registry).decode()


# ─── LOGIC: counter + histogram observed for a successful request ──────────


def test_successful_request_increments_counter_and_observes_latency() -> None:
    metrics = Metrics()
    response = _client(metrics).post(
        "/v1/chat/completions",
        json={
            "model": "selfhost-qwen",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 200

    dump = _scrape(metrics)
    assert (
        'llm_gateway_requests_total{method="POST",path="/v1/chat/completions",status="200"} 1.0'
        in dump
    )
    # Histogram emits *_count and *_sum lines per label set.
    assert (
        'llm_gateway_request_latency_seconds_count{method="POST",path="/v1/chat/completions"} 1.0'
        in dump
    )


# ─── LOGIC: skip-paths bypass the middleware ───────────────────────────────


def test_health_does_not_record_request_counter() -> None:
    metrics = Metrics()
    _client(metrics).get("/health")

    dump = _scrape(metrics)
    # No data line for /health — the middleware skips it entirely.
    # (/health appears in HELP descriptions; only label-line presence matters.)
    assert 'path="/health"' not in dump


def test_metrics_endpoint_does_not_record_itself() -> None:
    metrics = Metrics()
    client = _client(metrics)
    client.get("/metrics")
    client.get("/metrics")

    dump = _scrape(metrics)
    assert 'path="/metrics"' not in dump


# ─── LOGIC: unmatched routes label as "unmatched" (cardinality cap) ────────


def test_unknown_path_labels_as_unmatched() -> None:
    """A request to a route that does not match any handler must NOT
    inflate the path label cardinality with the raw URL — otherwise a
    404-spammer can OOM the gateway via /metrics."""
    metrics = Metrics()
    client = _client(metrics)

    response = client.get("/this/path/does/not/exist/" + "x" * 50)
    assert response.status_code == 404

    dump = _scrape(metrics)
    # The raw, attacker-controlled path must NEVER appear as a label.
    assert "/this/path/does/not/exist" not in dump
    # Cardinality stays bounded — unmatched requests collapse to one bucket.
    assert 'path="unmatched"' in dump


def test_known_route_labels_with_template_path() -> None:
    """The matched route's path template (a fixed string from the
    registered route table) is what lands in the Prometheus label."""
    metrics = Metrics()
    response = _client(metrics).post(
        "/v1/chat/completions",
        json={
            "model": "selfhost-qwen",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 200

    dump = _scrape(metrics)
    assert 'path="/v1/chat/completions"' in dump


# ─── LOGIC: token usage is incremented from response.usage ─────────────────


def test_token_counters_increment_from_response_usage() -> None:
    metrics = Metrics()
    backend = NoopBackend(
        canned_response={
            "id": "x",
            "object": "chat.completion",
            "model": "selfhost-qwen",
            "choices": [],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 7,
                "total_tokens": 19,
            },
        }
    )
    _client(metrics, backend=backend).post(
        "/v1/chat/completions",
        json={
            "model": "selfhost-qwen",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    dump = _scrape(metrics)
    assert 'llm_gateway_tokens_prompt_total{model="selfhost-qwen"} 12.0' in dump
    assert 'llm_gateway_tokens_completion_total{model="selfhost-qwen"} 7.0' in dump


def test_token_counters_skip_when_response_lacks_usage() -> None:
    """Streaming responses + custom backends may omit ``usage``; we
    simply don't record rather than crashing the request."""
    metrics = Metrics()
    backend = NoopBackend(canned_response={"id": "x", "choices": []})
    response = _client(metrics, backend=backend).post(
        "/v1/chat/completions",
        json={
            "model": "selfhost-qwen",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 200
    # Counter never observed — no metric line emitted.
    assert "llm_gateway_tokens_prompt_total" not in _scrape(metrics).split("# HELP")[1]


# ─── LOGIC: rate-limit rejection counter ───────────────────────────────────


def test_rate_limit_rejection_increments_counter() -> None:
    """RateLimitMiddleware fires ``on_rejection`` for every 429, which
    is wired to bump ``rate_limit_rejections_total``."""
    metrics = Metrics()

    class _AlwaysReject:
        async def check(self, key: str) -> Any:
            from llm_gateway.middleware.rate_limit import RateLimitDecision

            return RateLimitDecision(allowed=False, retry_after_s=1)

    app = create_app(
        settings=Settings(bearer_token=""),
        backend=NoopBackend(),
        metrics=metrics,
        rate_limiter=_AlwaysReject(),  # type: ignore[arg-type]
    )
    response = TestClient(app).post(
        "/v1/chat/completions",
        json={
            "model": "selfhost-qwen",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 429

    dump = _scrape(metrics)
    assert "llm_gateway_rate_limit_rejections_total 1.0" in dump
