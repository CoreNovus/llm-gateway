"""Health and readiness endpoints.

* ``GET /health`` — process is alive (200 always).
* ``GET /ready``  — engine is reachable + model loaded (200 / 503).

Modelled after the K8s ``livenessProbe`` / ``readinessProbe`` split: a
healthy process can still be unready (e.g. vLLM is loading 7B weights into
VRAM, ~30 s on a cold boot). Operators wire the SSH-tunnel smoke test to
``/ready`` so they only see green once the gateway is genuinely usable.
"""

# NOTE: do NOT add ``from __future__ import annotations`` to this module.
# FastAPI's ``Annotated[..., Depends(closure)]`` pattern needs eager
# evaluation of the Annotated metadata so it can inspect the embedded
# ``Depends`` instance; deferred-string evaluation drops the closure
# (the dependency callable is a function-local variable) and FastAPI
# silently re-classifies the parameter as a query argument → 422.

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from llm_gateway.inference.base import InferenceBackend


def make_health_router(backend_dep: Callable[[], InferenceBackend]) -> APIRouter:
    """Build the health router with the supplied backend dependency.

    ``backend_dep`` is a FastAPI dependency callable that resolves to an
    :class:`InferenceBackend`. Passing it as an argument (DIP) lets the
    test suite swap the backend without monkey-patching globals.
    """
    router = APIRouter(tags=["health"])

    @router.get("/health", status_code=status.HTTP_200_OK)
    async def health() -> dict[str, str]:
        """Process-liveness probe. Always 200 while the event loop runs."""
        return {"status": "ok"}

    @router.get("/ready")
    async def ready(
        backend: Annotated[InferenceBackend, Depends(backend_dep)],
    ) -> JSONResponse:
        """Readiness probe — backend.ping() must return ``True``."""
        if await backend.ping():
            return JSONResponse({"status": "ready"}, status_code=200)
        return JSONResponse(
            {"status": "unready"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return router
