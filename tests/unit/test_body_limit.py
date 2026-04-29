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


# ─── BOUNDARY: malformed Content-Length header treated as zero ─────────────


def test_malformed_content_length_header_treated_as_zero() -> None:
    """A bogus Content-Length is reset to 0; the body is then small enough
    to pass the cap. The protocol layer (Starlette) rejects truly broken
    framing earlier; we just don't crash on a non-int header value."""
    response = _client(max_bytes=1024).post(
        "/v1/anything", content="x" * 100, headers={"Content-Length": "not-an-int"}
    )
    assert response.status_code == 200
