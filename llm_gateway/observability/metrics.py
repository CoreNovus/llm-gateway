"""Prometheus metric instances grouped behind one ``Metrics`` class.

Why a class and not module-level singletons (the prometheus-client
default pattern)?

* **Per-instance registry** — tests construct a fresh ``Metrics`` each
  case and assert against ``CollectorRegistry`` snapshots without
  having to manually unregister between cases.
* **DIP** — middleware / endpoints / decorators receive the metrics
  via constructor injection instead of importing module-level globals,
  so they can be exercised with a stub ``Metrics`` (or skipped
  entirely with ``None``).
* **SRP** — every metric in one place: a future grep for "what does
  the gateway expose?" lands here, not scattered across files.

The metric names use the ``llm_gateway_`` prefix so a Prometheus operator
can scrape multiple gateways into the same dashboard without label
collisions with vLLM's own ``/metrics`` (which uses ``vllm_``).
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

# Bucket borders match the latency profile of LLM calls (median ~1s,
# tail multi-minute). The default prometheus-client bucket set tops out
# at 10s which would lose all chat-completion observations into +Inf.
_LLM_LATENCY_BUCKETS = (
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
)


class Metrics:
    """All gateway metrics in one container — one instance per process."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()

        self.requests_total = Counter(
            "llm_gateway_requests_total",
            "Total HTTP requests served, labelled by method / path / status.",
            labelnames=("method", "path", "status"),
            registry=self.registry,
        )

        self.request_latency_seconds = Histogram(
            "llm_gateway_request_latency_seconds",
            "End-to-end HTTP request latency (gateway side, includes upstream).",
            labelnames=("method", "path"),
            buckets=_LLM_LATENCY_BUCKETS,
            registry=self.registry,
        )

        self.inflight_requests = Gauge(
            "llm_gateway_inflight_requests",
            "Requests currently being served (excludes /metrics, /health, /ready).",
            registry=self.registry,
        )

        self.tokens_prompt_total = Counter(
            "llm_gateway_tokens_prompt_total",
            "Prompt tokens consumed by chat completions, labelled by model.",
            labelnames=("model",),
            registry=self.registry,
        )

        self.tokens_completion_total = Counter(
            "llm_gateway_tokens_completion_total",
            "Completion tokens emitted by chat completions, labelled by model.",
            labelnames=("model",),
            registry=self.registry,
        )

        self.rate_limit_rejections_total = Counter(
            "llm_gateway_rate_limit_rejections_total",
            "Requests rejected with 429 by the rate-limit middleware.",
            registry=self.registry,
        )

        self.circuit_breaker_state = Gauge(
            "llm_gateway_circuit_breaker_state",
            "Circuit breaker state (0=closed, 1=half-open, 2=open).",
            registry=self.registry,
        )
        # Initialise to 0 (closed) so dashboards have a value before the
        # first state transition.
        self.circuit_breaker_state.set(0)
