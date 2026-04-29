"""Per-request Prometheus instrumentation.

Records three series for every request that is not on the bypass list
(``/metrics``, ``/health``, ``/ready``):

* ``llm_gateway_requests_total{method, path, status}`` — counter
* ``llm_gateway_request_latency_seconds{method, path}`` — histogram
* ``llm_gateway_inflight_requests`` — gauge (incr on enter / decr on exit)

The middleware sits in the create_app stack between auth and rate-limit
so its observations cover authenticated traffic only. Unauth attempts
still land in the access log via :class:`RequestLoggingMiddleware`.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from llm_gateway.observability.metrics import Metrics


class MetricsMiddleware(BaseHTTPMiddleware):
    """Record per-request counter / histogram / in-flight gauge."""

    _DEFAULT_SKIP_PATHS: tuple[str, ...] = ("/metrics", "/health", "/ready")

    def __init__(
        self,
        app: ASGIApp,
        *,
        metrics: Metrics,
        skip_paths: Iterable[str] | None = None,
    ) -> None:
        super().__init__(app)
        self._metrics = metrics
        self._skip_paths = frozenset(skip_paths or self._DEFAULT_SKIP_PATHS)

    _UNMATCHED_LABEL = "unmatched"

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path in self._skip_paths:
            return await call_next(request)

        method = request.method
        self._metrics.inflight_requests.inc()
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            self._metrics.inflight_requests.dec()
            # Crashing handler: scope may not yet hold the matched route,
            # so label as "unmatched" to keep cardinality bounded. A
            # raw-path label here would let any client inflate the
            # Prometheus registry by hammering arbitrary URLs.
            self._metrics.request_latency_seconds.labels(
                method=method, path=self._UNMATCHED_LABEL
            ).observe(time.perf_counter() - start)
            self._metrics.requests_total.labels(
                method=method, path=self._UNMATCHED_LABEL, status="500"
            ).inc()
            raise

        self._metrics.inflight_requests.dec()
        elapsed = time.perf_counter() - start

        # Use the matched route template — bounded by registered routes —
        # rather than request.url.path. Without this, any 404-spammer
        # could OOM the gateway via /metrics cardinality blow-up.
        route = request.scope.get("route")
        label_path = getattr(route, "path", None) or self._UNMATCHED_LABEL

        self._metrics.request_latency_seconds.labels(method=method, path=label_path).observe(
            elapsed
        )
        self._metrics.requests_total.labels(
            method=method, path=label_path, status=str(response.status_code)
        ).inc()
        return response
