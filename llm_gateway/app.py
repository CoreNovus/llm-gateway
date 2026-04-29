"""FastAPI application factory.

``create_app`` is a pure function: it takes the dependencies it needs
(``settings``, ``backend``) as arguments and returns a fresh
:class:`FastAPI` instance. No module-level globals — tests construct an
app per case with a different backend.

This is the single place the package wires Protocol-level abstractions to
the FastAPI runtime. Middleware and routers register here without
touching any other file.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from llm_gateway.api.chat_completions import make_chat_completions_router
from llm_gateway.api.health import make_health_router
from llm_gateway.api.models_listing import make_models_listing_router
from llm_gateway.config import Settings
from llm_gateway.inference.base import InferenceBackend
from llm_gateway.middleware.auth import BearerAuthMiddleware
from llm_gateway.middleware.body_limit import BodySizeLimitMiddleware
from llm_gateway.middleware.logging import RequestLoggingMiddleware
from llm_gateway.middleware.metrics import MetricsMiddleware
from llm_gateway.middleware.rate_limit import (
    InMemoryTokenBucket,
    RateLimiter,
    RateLimitMiddleware,
)
from llm_gateway.observability.metrics import Metrics
from llm_gateway.observability.router import make_metrics_router


def create_app(
    settings: Settings,
    backend: InferenceBackend,
    *,
    rate_limiter: RateLimiter | None = None,
    metrics: Metrics | None = None,
) -> FastAPI:
    """Build a FastAPI instance wired to the supplied backend.

    Args:
        settings: Already-constructed :class:`Settings` instance. The
            factory does not read environment variables — keeps the
            test seam single-pointed.
        backend: Concrete :class:`InferenceBackend`. Production wires
            this to :class:`VLLMHTTPBackend`; tests pass
            :class:`NoopBackend`.
        rate_limiter: Optional :class:`RateLimiter` override for tests.
            Defaults to :class:`InMemoryTokenBucket` reading
            ``settings.rate_limit_rpm``.
        metrics: Optional :class:`Metrics` override for tests / shared
            registries. Defaults to a fresh :class:`Metrics` per app —
            the ``/metrics`` endpoint serves this instance's registry.
    """
    metrics = metrics or Metrics()

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Startup phase is currently empty (the backend is constructed
        # by the caller and passed in). On SIGTERM uvicorn waits for
        # in-flight requests to drain, then enters the shutdown phase
        # below; we release the backend's connection pool so the
        # process exits cleanly.
        try:
            yield
        finally:
            await backend.aclose()

    app = FastAPI(
        title="llm-gateway gateway",
        version="0.1.0",
        lifespan=_lifespan,
        # Docs default to /docs; we keep them on for local dev. The SSH
        # tunnel is the auth boundary so docs are not publicly reachable.
    )

    # FastAPI ``Depends`` resolves the same ``backend`` instance into
    # any handler that asks for it (DIP — handlers depend on the
    # Protocol, not on the concrete class).
    def _backend_dep() -> InferenceBackend:
        return backend

    app.include_router(make_health_router(_backend_dep))
    app.include_router(make_models_listing_router())
    app.include_router(make_chat_completions_router(_backend_dep, metrics=metrics))
    app.include_router(make_metrics_router(metrics))

    # ── Middleware order ────────────────────────────────────────────────
    # ``add_middleware`` applies in REVERSE registration order, so the
    # last call here ends up outermost. We want:
    #   1. Logging      outermost — observe everything, including 401 / 429
    #   2. BodyLimit    next     — drop oversized bodies before auth /
    #                              pydantic ever consume memory
    #   3. Auth         middle   — fail unauth before charging the bucket
    #   4. RateLim      middle   — only authed traffic counts against quota
    #   5. Metrics      innermost — only allowed requests reach handlers,
    #                              so record latency / status of the work
    #                              that actually happened.
    limiter = rate_limiter or InMemoryTokenBucket(
        rpm=settings.rate_limit_rpm,
        max_keys=settings.rate_limit_max_keys,
        idle_evict_s=settings.rate_limit_idle_evict_s,
    )
    app.add_middleware(MetricsMiddleware, metrics=metrics)
    app.add_middleware(
        RateLimitMiddleware,
        limiter=limiter,
        on_rejection=metrics.rate_limit_rejections_total.inc,
    )
    app.add_middleware(BearerAuthMiddleware, bearer_token=settings.bearer_token)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(RequestLoggingMiddleware)

    # Stash the settings on app.state so future routers can read them
    # without re-importing the config module.
    app.state.settings = settings

    return app
