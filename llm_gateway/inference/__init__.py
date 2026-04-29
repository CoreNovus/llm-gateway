"""Inference backend abstractions."""

from llm_gateway.inference.base import InferenceBackend
from llm_gateway.inference.circuit_breaker import CircuitBreakerBackend, CircuitState
from llm_gateway.inference.errors import (
    InferenceError,
    UpstreamClientError,
    UpstreamUnavailableError,
)
from llm_gateway.inference.noop import NoopBackend
from llm_gateway.inference.vllm_http import VLLMHTTPBackend

__all__ = [
    "CircuitBreakerBackend",
    "CircuitState",
    "InferenceBackend",
    "InferenceError",
    "NoopBackend",
    "UpstreamClientError",
    "UpstreamUnavailableError",
    "VLLMHTTPBackend",
]
