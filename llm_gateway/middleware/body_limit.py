"""Reject requests whose body exceeds a configured byte cap.

FastAPI / Starlette do not enforce a default body cap; an unbounded
``messages`` array on ``/v1/chat/completions`` could blow process
memory before pydantic ever sees it. This middleware reads the
``Content-Length`` header (set by every well-behaved HTTP client) and
returns ``413 Payload Too Large`` before the body is consumed when it
exceeds ``max_bytes``.

We do NOT attempt to gate chunked / non-Content-Length requests here:

* Starlette buffers chunked uploads into memory the same way; if the
  caller is hostile enough to send a huge chunked body, we still cap
  via uvicorn's ``--limit-max-requests`` / kernel-level limits at
  deploy time.
* Honest clients (httpx, openai-python, langchain-openai, curl) all
  set Content-Length on JSON POSTs.

Skip paths mirror auth / rate-limit defaults (``/metrics``, ``/health``,
``/ready``) so probes never see 413.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp


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
        if raw is not None:
            try:
                declared = int(raw)
            except ValueError:
                # Malformed header — let the protocol layer reject it.
                declared = 0
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
