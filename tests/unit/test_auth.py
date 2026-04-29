"""Bearer auth middleware tests.

Mounts the middleware on a tiny FastAPI app with a single ``/protected``
endpoint so behaviour is observable in isolation — no /v1/chat/completions
plumbing needed.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway.middleware.auth import BearerAuthMiddleware


def _app(*, bearer_token: str) -> TestClient:
    app = FastAPI()
    app.add_middleware(BearerAuthMiddleware, bearer_token=bearer_token)

    @app.get("/protected")
    def protected() -> dict[str, str]:
        return {"ok": "true"}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return TestClient(app)


# ─── ERROR: missing / wrong token ───────────────────────────────────────────


def test_missing_authorization_header_returns_401() -> None:
    response = _app(bearer_token="placeholder-bearer").get("/protected")  # pragma: allowlist secret
    assert response.status_code == 401
    assert response.json() == {"error": "missing_bearer_token"}


def test_non_bearer_authorization_header_returns_401() -> None:
    response = _app(bearer_token="placeholder-bearer").get(  # pragma: allowlist secret
        "/protected", headers={"Authorization": "Basic abc"}
    )
    assert response.status_code == 401
    assert response.json() == {"error": "missing_bearer_token"}


def test_wrong_bearer_token_returns_401() -> None:
    response = _app(bearer_token="placeholder-bearer").get(  # pragma: allowlist secret
        "/protected", headers={"Authorization": "Bearer wrong"}
    )
    assert response.status_code == 401
    assert response.json() == {"error": "invalid_bearer_token"}


# ─── LOGIC: valid token + bypasses ──────────────────────────────────────────


def test_valid_bearer_token_lets_request_through() -> None:
    response = _app(bearer_token="placeholder-bearer").get(  # pragma: allowlist secret
        "/protected", headers={"Authorization": "Bearer placeholder-bearer"}
    )
    assert response.status_code == 200
    assert response.json() == {"ok": "true"}


def test_health_path_bypasses_auth() -> None:
    """Probes must always succeed even with no Authorization header."""
    response = _app(bearer_token="placeholder-bearer").get("/health")  # pragma: allowlist secret
    assert response.status_code == 200


def test_empty_configured_token_disables_auth() -> None:
    """Dev-mode bypass — production CDK refuses to deploy without a token."""
    response = _app(bearer_token="").get("/protected")
    assert response.status_code == 200
