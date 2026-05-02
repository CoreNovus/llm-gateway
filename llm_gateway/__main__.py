"""Entry point: ``python -m llm_gateway`` boots the gateway with uvicorn.

Wires the real :class:`VLLMHTTPBackend` wrapped in a circuit breaker.
Tests construct a :class:`NoopBackend` and call :func:`create_app`
directly — they never go through this module.
"""

from __future__ import annotations

import uvicorn

from llm_gateway.app import create_app
from llm_gateway.config import Settings, get_settings
from llm_gateway.inference.base import InferenceBackend
from llm_gateway.inference.circuit_breaker import CircuitBreakerBackend, CircuitState
from llm_gateway.inference.vllm_http import VLLMHTTPBackend
from llm_gateway.observability.metrics import Metrics

_CIRCUIT_STATE_GAUGE: dict[CircuitState, int] = {
    "closed": 0,
    "half-open": 1,
    "open": 2,
}


def _require_bearer_token(token: str) -> None:
    """Refuse to start the production server when no bearer token is set.

    The auth middleware (``BearerAuthMiddleware``) treats an empty
    token as "dev-mode bypass" so unit tests + local NoopBackend
    smoke runs work without configuring a secret. That is intentional
    only for callers that bypass ``__main__`` (i.e. the test suite,
    which constructs ``create_app`` directly). When the operator runs
    ``python -m llm_gateway``, an empty token would silently disable auth
    on the gateway — caught here loudly instead.
    """
    if not token:
        raise RuntimeError(
            "BEARER_TOKEN must be set. Empty token disables auth — "
            "if that is what you want for local dev, call create_app() "
            "directly with a NoopBackend instead of `python -m llm_gateway`."
        )


def _build_backend(settings: Settings, metrics: Metrics) -> InferenceBackend:
    """Compose the inference backend from settings.

    Pure function so tests can call it without booting uvicorn. The
    circuit breaker is wrapped LAST so it observes everything every
    inner layer raises; its state callback updates the gauge so the
    /metrics endpoint reflects current breaker state.
    """
    backend: InferenceBackend = VLLMHTTPBackend(
        upstream_url=settings.vllm_upstream_url,
        request_timeout_s=settings.vllm_request_timeout_s,
    )
    if settings.circuit_breaker_enabled:

        def _on_state_change(state: CircuitState) -> None:
            metrics.circuit_breaker_state.set(_CIRCUIT_STATE_GAUGE[state])

        backend = CircuitBreakerBackend(
            backend,
            failure_threshold=settings.circuit_breaker_failure_threshold,
            cooldown_s=settings.circuit_breaker_cooldown_s,
            on_state_change=_on_state_change,
        )
    return backend


def main() -> None:
    """Production entry — wires the real vLLM HTTP proxy backend.

    Tests construct a :class:`NoopBackend` and pass it directly to
    :func:`create_app`. ``/ready`` returns 503 while vLLM is
    unreachable, which is the correct status — operators look at
    /ready to know when the stack is genuinely usable.

    Uvicorn hardening flags below are *defence in depth* on top of the
    127.0.0.1 bind + SSH-tunnel boundary documented in README.md.
    They make sure that if anyone ever lifts the gateway off loopback
    without re-reading the threat model, uvicorn does not silently
    trust X-Forwarded-* headers from arbitrary callers and does not
    leak the server stack version through response headers.
    """
    settings = get_settings()
    _require_bearer_token(settings.bearer_token)
    metrics = Metrics()
    backend = _build_backend(settings, metrics)
    app = create_app(settings=settings, backend=backend, metrics=metrics)

    uvicorn.run(
        app,
        host=settings.server_host,
        port=settings.server_port,
        log_level=settings.log_level.lower(),
        # Never trust X-Forwarded-* — the gateway is the trust boundary.
        # If a future deploy puts a real proxy in front, the operator
        # must explicitly opt in by setting these to True + naming the
        # proxy IPs in ``forwarded_allow_ips``.
        proxy_headers=False,
        forwarded_allow_ips="127.0.0.1",
        # Drop ``Server: uvicorn`` and ``Date`` from responses. The
        # version string is fingerprintable (CVE catalogue lookup);
        # ``Date`` is redundant with the operator's own log timestamps.
        server_header=False,
        date_header=False,
    )


if __name__ == "__main__":
    main()
