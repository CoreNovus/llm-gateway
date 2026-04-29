"""Prometheus metrics — collection + ``/metrics`` endpoint."""

from llm_gateway.observability.metrics import Metrics
from llm_gateway.observability.router import make_metrics_router

__all__ = ["Metrics", "make_metrics_router"]
