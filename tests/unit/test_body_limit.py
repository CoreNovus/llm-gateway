"""``BodySizeLimitMiddleware`` tests."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway.middleware.body_limit import BodySizeLimitMiddleware


def _client(*, max_bytes: int = 1024) -> TestClient:
    app = FastAPI()
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_bytes)

    @app.post("/v1/anything")
    def anything() -> dict[str, str]:
        return {"ok": "true"}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return TestClient(app)


# ─── ERROR: invalid construction ────────────────────────────────────────────


def test_zero_max_bytes_rejected() -> None:
    app = FastAPI()
    with pytest.raises(ValueError, match="max_bytes"):
        app.add_middleware(BodySizeLimitMiddleware, max_bytes=0)
        # add_middleware defers; force the build by issuing a request.
        TestClient(app).get("/")


# ─── LOGIC: cap enforced for oversized POST ─────────────────────────────────


def test_oversized_post_returns_413() -> None:
    payload = "x" * 2048
    response = _client(max_bytes=1024).post("/v1/anything", content=payload)
    assert response.status_code == 413
    body = response.json()
    assert body["error"]["code"] == "payload_too_large"
    assert "1024" in body["error"]["message"]


def test_within_limit_post_passes_through() -> None:
    response = _client(max_bytes=1024).post("/v1/anything", content="x" * 100)
    assert response.status_code == 200


# ─── LOGIC: skip-paths bypass the middleware ───────────────────────────────


def test_health_path_is_never_capped() -> None:
    response = _client(max_bytes=1).get("/health")
    assert response.status_code == 200


# ─── BOUNDARY: malformed Content-Length header is now rejected ─────────────


def test_malformed_content_length_header_returns_400() -> None:
    """A non-int Content-Length is refused with 400. Previously the
    middleware silently zeroed the value and forwarded the request,
    letting a hostile client bypass the cap by sending an unparseable
    header (the body could be arbitrarily large, the cap-comparison
    saw 0)."""
    response = _client(max_bytes=1024).post(
        "/v1/anything", content="x" * 100, headers={"Content-Length": "not-an-int"}
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_content_length"


def test_negative_content_length_returns_400() -> None:
    """A syntactically-valid but negative Content-Length is also refused."""
    response = _client(max_bytes=1024).post(
        "/v1/anything", content="x" * 100, headers={"Content-Length": "-1"}
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_content_length"
