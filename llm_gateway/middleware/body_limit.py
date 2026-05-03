"""Reject requests whose body exceeds a configured byte cap.

FastAPI / Starlette do not enforce a default body cap; an unbounded
``messages`` array on ``/v1/chat/completions`` could blow process
memory before pydantic ever sees it. This middleware enforces a cap
at the header layer:

* ``Content-Length`` present and over the cap → ``413 Payload Too Large``
  before the body is consumed.
* ``Content-Length`` malformed (e.g. ``"abc"``) → ``400 Bad Request``.
  Previously we silently zeroed the value and forwarded the request,
  which let a hostile client bypass the cap by sending an unparseable
  header.
* ``Content-Length`` negative → ``400 Bad Request``.
* ``Content-Length`` missing on a body-bearing method (POST / PUT /
  PATCH) → ``411 Length Required``. Without a declared length the cap
  cannot be enforced at the header layer (chunked transfer-encoding
  bodies have no Content-Length); honest JSON clients (httpx,
  openai-python, langchain-openai, curl) all set Content-Length, so
  the practical impact is zero. Operators who genuinely need to
  accept chunked uploads must front the gateway with a proxy that
  sets Content-Length, or extend this middleware to a streaming
  ASGI implementation.

Skip paths mirror auth / rate-limit defaults (``/metrics``, ``/health``,
``/ready``) so probes never see 411 / 413.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

# RFC 9110 §9.3: methods that may have a request body.
_BODY_BEARING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH"})


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose declared Content-Length exceeds ``max_bytes``."""

    _DEFAULT_SKIP_PATHS: tuple[str, ...] = ("/metrics", "/health", "/ready")

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        skip_paths: Iterable[str] | None = None,
    ) -> None:
        super().__init__(app)
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._max_bytes = max_bytes
        self._skip_paths = frozenset(skip_paths or self._DEFAULT_SKIP_PATHS)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path in self._skip_paths:
            return await call_next(request)

        raw = request.headers.get("content-length")
        method = request.method.upper()

        if raw is None:
            # No Content-Length. For body-bearing methods, refuse — the
            # only way a non-chunked request omits Content-Length is by
            # accident (libcurl with --upload-file from stdin etc.); the
            # only way a chunked request includes a body is via
            # ``Transfer-Encoding: chunked`` whose total length is
            # unknown to this middleware. Either way the cap cannot be
            # enforced, so we refuse loudly with 411.
            if method in _BODY_BEARING_METHODS:
                return JSONResponse(
                    {
                        "error": {
                            "code": "length_required",
                            "message": (
                                f"Content-Length header is required for {method} "
                                "requests; chunked transfer-encoding is not accepted."
                            ),
                        }
                    },
                    status_code=411,
                    headers={"Connection": "close"},
                )
            return await call_next(request)

        try:
            declared = int(raw)
        except ValueError:
            # Refuse rather than zero-and-forward. A hostile client
            # sending ``Content-Length: abc`` got a free pass under the
            # old behaviour.
            return JSONResponse(
                {
                    "error": {
                        "code": "invalid_content_length",
                        "message": f"Content-Length header is malformed: {raw!r}",
                    }
                },
                status_code=400,
                headers={"Connection": "close"},
            )

        if declared < 0:
            return JSONResponse(
                {
                    "error": {
                        "code": "invalid_content_length",
                        "message": "Content-Length must be non-negative.",
                    }
                },
                status_code=400,
                headers={"Connection": "close"},
            )

        if declared > self._max_bytes:
            return JSONResponse(
                {
                    "error": {
                        "code": "payload_too_large",
                        "message": (
                            f"Request body declared {declared} bytes "
                            f"exceeds the gateway cap of {self._max_bytes}."
                        ),
                    }
                },
                status_code=413,
                headers={"Connection": "close"},
            )

        return await call_next(request)
