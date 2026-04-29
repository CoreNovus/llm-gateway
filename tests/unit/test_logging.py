"""Logging middleware + correlation-id tests."""

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway.middleware.correlation import (
    CORRELATION_HEADER,
    current_correlation_id,
)
from llm_gateway.middleware.logging import RequestLoggingMiddleware


def _client(custom_endpoints: bool = False) -> TestClient:
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/anything")
    def anything() -> dict[str, str]:
        return {"correlation_id": current_correlation_id() or ""}

    if custom_endpoints:

        @app.get("/boom")
        def boom() -> None:
            raise RuntimeError("kaboom")

    return TestClient(app, raise_server_exceptions=False)


# ─── LOGIC: correlation-id forwarding ──────────────────────────────────────


def test_correlation_id_from_inbound_header_is_used_and_echoed() -> None:
    response = _client().get("/anything", headers={CORRELATION_HEADER: "abc-123"})
    assert response.headers[CORRELATION_HEADER] == "abc-123"
    assert response.json() == {"correlation_id": "abc-123"}


def test_missing_correlation_id_is_generated_and_echoed() -> None:
    response = _client().get("/anything")
    correlation_id = response.headers[CORRELATION_HEADER]
    assert correlation_id  # generated, non-empty
    assert response.json() == {"correlation_id": correlation_id}


def test_each_request_gets_an_independent_correlation_id() -> None:
    client = _client()
    a = client.get("/anything").headers[CORRELATION_HEADER]
    b = client.get("/anything").headers[CORRELATION_HEADER]
    assert a != b


# ─── inbound correlation-id validation ─────────────────────────────────────


def test_oversized_inbound_correlation_id_is_replaced_with_uuid() -> None:
    """A 200-char inbound header is rejected by the regex (cap = 128) —
    the response carries a fresh uuid4 instead of the garbage."""
    garbage = "a" * 200
    response = _client().get("/anything", headers={CORRELATION_HEADER: garbage})
    echoed = response.headers[CORRELATION_HEADER]
    assert echoed != garbage
    assert len(echoed) <= 128


def test_control_chars_in_inbound_correlation_id_are_replaced() -> None:
    """Log-injection attempt — CR/LF / null bytes in the header are
    replaced. (Starlette's HTTP parser also rejects these at the wire,
    but defence-in-depth.)"""
    response = _client().get("/anything", headers={CORRELATION_HEADER: "abc\twith\ttabs"})
    echoed = response.headers[CORRELATION_HEADER]
    assert "\t" not in echoed
    assert echoed != "abc\twith\ttabs"


def test_empty_inbound_correlation_id_is_replaced() -> None:
    response = _client().get("/anything", headers={CORRELATION_HEADER: ""})
    echoed = response.headers[CORRELATION_HEADER]
    assert echoed  # generated, non-empty


# ─── OBJECT-STATE: structured log emission ─────────────────────────────────


def test_completed_request_emits_structured_access_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="llm_gateway.access")
    _client().get("/anything", headers={CORRELATION_HEADER: "trace-xyz"})

    matching = [r for r in caplog.records if r.message == "request completed"]
    assert len(matching) == 1
    record = matching[0]
    assert record.correlation_id == "trace-xyz"  # type: ignore[attr-defined]
    assert record.method == "GET"  # type: ignore[attr-defined]
    assert record.path == "/anything"  # type: ignore[attr-defined]
    assert record.status == 200  # type: ignore[attr-defined]
    assert isinstance(record.latency_ms, float)  # type: ignore[attr-defined]


def test_handler_exception_emits_failed_log_then_propagates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="llm_gateway.access")
    response = _client(custom_endpoints=True).get(
        "/boom", headers={CORRELATION_HEADER: "trace-fail"}
    )
    # Starlette returns 500 with raise_server_exceptions=False.
    assert response.status_code == 500

    failed = [r for r in caplog.records if r.message == "request failed"]
    assert len(failed) == 1
    assert failed[0].correlation_id == "trace-fail"  # type: ignore[attr-defined]


# ─── redacting filter on the access logger ─────────────────────────────────


def test_redacting_filter_replaces_sensitive_extra_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Defence-in-depth — even if a future caller adds extra= with a
    sensitive key, the filter rewrites the value before the formatter
    sees it."""
    caplog.set_level(logging.INFO, logger="llm_gateway.access")
    access_logger = logging.getLogger("llm_gateway.access")

    access_logger.info(
        "test event",
        extra={
            "authorization": "Bearer placeholder-bearer",  # pragma: allowlist secret
            "api_key": "not-a-real-api-key",  # pragma: allowlist secret
            "BEARER_TOKEN": "x",  # pragma: allowlist secret
            "User-Token": "y",  # pragma: allowlist secret
            "Secret-Header": "z",  # pragma: allowlist secret
            "password": "p",  # pragma: allowlist secret
            "request_id": "preserved",  # not sensitive — kept as-is
        },
    )

    record = next(r for r in caplog.records if r.message == "test event")
    # Sensitive keys all replaced.
    assert record.authorization == "<redacted>"  # type: ignore[attr-defined]
    assert record.api_key == "<redacted>"  # type: ignore[attr-defined]
    assert record.BEARER_TOKEN == "<redacted>"
    assert getattr(record, "User-Token") == "<redacted>"
    assert getattr(record, "Secret-Header") == "<redacted>"
    assert record.password == "<redacted>"  # type: ignore[attr-defined]
    # Non-sensitive key untouched.
    assert record.request_id == "preserved"  # type: ignore[attr-defined]


def test_redacting_filter_does_not_touch_standard_logrecord_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The filter must not corrupt LogRecord built-ins (msg, module,
    etc.) just because their names happen to brush against the
    sensitive-key regex."""
    caplog.set_level(logging.INFO, logger="llm_gateway.access")
    logging.getLogger("llm_gateway.access").info("hello world")

    record = next(r for r in caplog.records if r.message == "hello world")
    assert record.message == "hello world"
    assert record.levelname == "INFO"
