"""Bearer-token auth middleware.

The gateway's only auth surface — the SSH tunnel is the network-level
boundary. Compares the inbound ``Authorization: Bearer <token>`` against
the configured token using a constant-time comparison.

Empty configured token disables auth (intentional — local dev with the
NoopBackend doesn't need a real token). Production CDK refuses to deploy
without a populated Secrets Manager value, so an empty token can only
appear during local development.

Skip paths: ``/health`` and ``/ready`` always bypass auth so K8s-style
liveness / readiness probes work without credentials.
"""

import hmac
from collections.abc import Awaitable, Callable, Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject non-skip-path requests missing a valid bearer token."""

    _DEFAULT_SKIP_PATHS: tuple[str, ...] = ("/metrics", "/health", "/ready")

    def __init__(
        self,
        app: ASGIApp,
        *,
        bearer_token: str,
        skip_paths: Iterable[str] | None = None,
    ) -> None:
        super().__init__(app)
        self._bearer_token = bearer_token
        self._skip_paths = frozenset(skip_paths or self._DEFAULT_SKIP_PATHS)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path in self._skip_paths:
            return await call_next(request)

        if not self._bearer_token:
            # Dev mode — auth disabled. CDK ensures the deployed gateway
            # always has a token, so this branch only fires locally.
            return await call_next(request)

        header = request.headers.get("authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return JSONResponse(
                {"error": "missing_bearer_token"},
                status_code=401,
            )

        provided = header[len(prefix) :].strip()
        # Constant-time comparison defeats timing oracles regardless of
        # whether the token is per-tenant or a single shared secret.
        if not hmac.compare_digest(provided, self._bearer_token):
            return JSONResponse(
                {"error": "invalid_bearer_token"},
                status_code=401,
            )
        return await call_next(request)
