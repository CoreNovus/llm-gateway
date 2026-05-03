"""Structured request logging — outermost middleware.

Logs every request including auth failures and rate-limit rejections (so
security teams can spot scanners) under the ``llm_gateway.access`` logger.
The operator picks the formatter / sink at the host level; the middleware
emits plain ``logging.Logger`` records with structured ``extra`` fields
so a JSON formatter can pick them up without changes here.

The middleware also anchors the correlation-id contract: read from the
inbound header, fall back to a fresh uuid4, stamp on the response so
the client sees the same id we logged.

Defence-in-depth: a ``_RedactingFilter`` on the access logger strips
``extra`` fields whose name matches a sensitive-key pattern
(authorization, bearer, api_key, token, secret, password). The
middleware logs only known-safe fields, but any future caller adding
``extra={"authorization": ...}`` cannot leak — the filter rewrites the
value to ``<redacted>`` before the formatter runs.
"""

import logging
import re
import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from llm_gateway.middleware.correlation import (
    CORRELATION_HEADER,
    normalise_inbound_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)

_SENSITIVE_KEY_PATTERN = re.compile(
    r"authorization|bearer|api[_-]?key|token|secret|password",
    re.IGNORECASE,
)
# Standard LogRecord attributes — never redacted (a key called ``msg`` or
# ``module`` happens to match would otherwise corrupt every log line).
_LOGRECORD_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None)))

# Strip ASCII control characters (CR / LF / NUL / others) from log fields
# derived from inbound request data. With a JSON formatter the risk is
# contained, but with the stdlib default formatter a CR/LF in
# ``request.url.path`` forges fake log lines that confuse SIEM parsers.
# Replace with U+FFFD (replacement char) to keep the value visibly broken
# without dropping evidence of the attempt.
_LOG_INJECTION_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _safe_log_value(value: str) -> str:
    """Return ``value`` with ASCII control characters replaced by U+FFFD."""
    return _LOG_INJECTION_CHARS.sub("�", value)


class _RedactingFilter(logging.Filter):
    """Replace sensitive ``extra`` field values with ``<redacted>``.

    Only inspects user-supplied attributes (those NOT present on a
    blank :class:`logging.LogRecord`); standard fields like ``msg``,
    ``module``, ``levelname`` are untouched. The filter is idempotent
    and side-effect-free apart from the targeted ``setattr`` calls.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for attr in list(vars(record)):
            if attr in _LOGRECORD_RESERVED:
                continue
            if _SENSITIVE_KEY_PATTERN.search(attr):
                setattr(record, attr, "<redacted>")
        return True


_logger = logging.getLogger("llm_gateway.access")
# Idempotent: ``addFilter`` ignores the call when an instance with the
# same identity is already registered. We use a module-level singleton
# so re-import (e.g. test reloads) does not stack filters.
_REDACTING_FILTER = _RedactingFilter()
if not any(isinstance(f, _RedactingFilter) for f in _logger.filters):
    _logger.addFilter(_REDACTING_FILTER)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Outermost middleware — logs every request with a correlation_id."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        correlation_id = normalise_inbound_correlation_id(request.headers.get(CORRELATION_HEADER))
        token = set_correlation_id(correlation_id)
        # Strip control chars from any inbound string we are about to
        # interpolate into a log line. ``request.method`` is a small
        # whitelisted set in practice but defended uniformly so a future
        # field added here cannot regress.
        safe_method = _safe_log_value(request.method)
        safe_path = _safe_log_value(request.url.path)

        start = time.perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception:
                latency_ms = round((time.perf_counter() - start) * 1000, 1)
                # The exc_info is captured automatically by .exception().
                _logger.exception(
                    "request failed",
                    extra={
                        "correlation_id": correlation_id,
                        "method": safe_method,
                        "path": safe_path,
                        "latency_ms": latency_ms,
                    },
                )
                raise

            latency_ms = round((time.perf_counter() - start) * 1000, 1)
            _logger.info(
                "request completed",
                extra={
                    "correlation_id": correlation_id,
                    "method": safe_method,
                    "path": safe_path,
                    "status": response.status_code,
                    "latency_ms": latency_ms,
                },
            )
            response.headers[CORRELATION_HEADER] = correlation_id
            return response
        finally:
            # Reset the contextvar so the id does not leak into
            # background tasks / post-response cleanup that runs in the
            # same asyncio task. ``set_correlation_id`` returned a Token
            # whose ``reset`` restores the prior value.
            reset_correlation_id(token)
