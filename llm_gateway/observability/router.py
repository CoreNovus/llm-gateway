"""``GET /metrics`` — Prometheus exposition endpoint.

The endpoint is intentionally NOT in ``/v1`` and intentionally bypasses
auth + rate-limit (via the auth / rate-limit middleware skip_paths) so
a Prometheus scraper does not need a bearer token. Combined with the
gateway binding to ``127.0.0.1``, only operators with SSH access can
scrape — same boundary as the rest of the gateway.
"""

from fastapi import APIRouter
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from llm_gateway.observability.metrics import Metrics


def make_metrics_router(metrics: Metrics) -> APIRouter:
    router = APIRouter(tags=["observability"])

    @router.get("/metrics")
    def metrics_endpoint() -> Response:
        return Response(
            content=generate_latest(metrics.registry),
            media_type=CONTENT_TYPE_LATEST,
        )

    return router
