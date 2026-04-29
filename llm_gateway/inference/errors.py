"""Inference-layer exceptions translated to HTTP by the API layer.

Two narrow categories so the API layer's mapping is exhaustive:

* :class:`UpstreamUnavailableError` — the engine is unreachable, timing
  out, or returning 5xx. Maps to HTTP 502 Bad Gateway with our error
  envelope. The caller is not at fault; retry-with-backoff is the
  expected response.
* :class:`UpstreamClientError` — the engine returned 4xx (bad model id,
  malformed request, validation rejection). Maps to the upstream's
  exact status with the upstream's body so the caller sees the real
  error message.

Anything else propagates as ``500`` and lands in the access log via
:class:`RequestLoggingMiddleware` for the operator to triage.
"""

from __future__ import annotations

from typing import Any


class InferenceError(Exception):
    """Base for inference-backend errors translated to HTTP status codes."""


class UpstreamUnavailableError(InferenceError):
    """Engine unreachable, timing out, or returning 5xx."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UpstreamClientError(InferenceError):
    """Engine returned 4xx — caller is at fault, forward status + body."""

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        super().__init__(f"upstream client error {status_code}: {body}")
        self.status_code = status_code
        self.body = body
