"""``GET /v1/models`` — list models the gateway is willing to serve.

OpenAI clients call this to discover names. We surface the static
:data:`DEFAULT_REGISTRY`; what is actually loaded on the GPU at any
moment is a separate concern — the circuit breaker flips ``/ready``
to 503 if the listed model is not actually live.
"""

from __future__ import annotations

from fastapi import APIRouter

from llm_gateway.models.registry import DEFAULT_REGISTRY


def make_models_listing_router() -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["models"])

    @router.get("/models")
    async def list_models() -> dict[str, object]:
        return {
            "object": "list",
            "data": [
                {
                    "id": defn.served_name,
                    "object": "model",
                    "owned_by": "self-hosted",
                }
                for defn in DEFAULT_REGISTRY.values()
            ],
        }

    return router
