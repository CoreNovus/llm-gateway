"""Circuit-breaker decorator around an :class:`InferenceBackend`.

Protects the gateway against a repeatedly-failing upstream (vLLM
crashed, GPU OOM, container restarting) by opening the breaker after
``failure_threshold`` consecutive :class:`UpstreamUnavailableError`s.
While the breaker is open, requests fail fast — the gateway returns
``UpstreamUnavailableError`` synchronously without ever calling the
upstream. After ``cooldown_s`` the breaker enters *half-open*: the
next request is a probe; success closes the breaker, failure re-opens
it.

Design notes:

* Decorator pattern. The wrapper IS-A ``InferenceBackend`` — the API
  layer is unaware it exists.
* Only :class:`UpstreamUnavailableError` counts toward the failure
  budget. :class:`UpstreamClientError` is the caller's fault (bad
  request) and must not break the circuit on the engine's behalf.
* :meth:`ping` always probes the inner backend regardless of state
  — readiness must reflect actual reachability, not historical
  failures. (If you want a cached ping, layer it separately.)
* State transitions are observable via :meth:`state` and via the
  optional ``on_state_change`` callback so the ``/metrics`` gauge
  can mirror current state without polling.

Production wiring is one line in :func:`__main__.main`:

    backend = CircuitBreakerBackend(VLLMHTTPBackend(...), ...)
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal

from llm_gateway.inference.base import InferenceBackend
from llm_gateway.inference.errors import UpstreamUnavailableError

CircuitState = Literal["closed", "open", "half-open"]


class CircuitBreakerBackend:
    """Decorator that opens a breaker on consecutive upstream failures."""

    def __init__(
        self,
        inner: InferenceBackend,
        *,
        failure_threshold: int = 5,
        cooldown_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        on_state_change: Callable[[CircuitState], None] | None = None,
    ) -> None:
        if failure_threshold <= 0:
            raise ValueError("failure_threshold must be positive")
        if cooldown_s <= 0:
            raise ValueError("cooldown_s must be positive")
        self._inner = inner
        self._failure_threshold = failure_threshold
        self._cooldown_s = cooldown_s
        self._clock = clock
        # Optional callback fired whenever the breaker transitions from
        # one observed state to another. The metrics layer wires it to
        # a gauge; tests assert state changes via this hook without
        # poking the private state machine.
        self._on_state_change = on_state_change
        self._last_observed_state: CircuitState = "closed"
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    def state(self) -> CircuitState:
        """Return the current breaker state.

        ``"closed"``  — normal traffic, all calls reach upstream.
        ``"open"``    — fail-fast; calls raise without touching upstream.
        ``"half-open"`` — cooldown elapsed; next call probes upstream.
        """
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self._cooldown_s:
            return "half-open"
        return "open"

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def ping(self) -> bool:
        # Always actually probe — ping is meant to discover state and
        # has its own short timeout. Cached state would lie.
        return await self._inner.ping()

    async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        self._raise_if_open()
        try:
            result = await self._inner.complete(request)
        except UpstreamUnavailableError:
            self._record_failure()
            raise
        # UpstreamClientError propagates without counting (4xx is the
        # caller's fault, not the engine's).
        self._record_success()
        return result

    async def stream(self, request: dict[str, Any]) -> AsyncIterator[bytes]:
        self._raise_if_open()
        try:
            iterator = await self._inner.stream(request)
        except UpstreamUnavailableError:
            self._record_failure()
            raise
        self._record_success()
        return iterator

    # ── internal state machine ──────────────────────────────────────────

    def _raise_if_open(self) -> None:
        # ``state()`` may transition open → half-open silently when the
        # cooldown elapses; emit a hook for that too so dashboards see
        # the gauge drop without waiting for the next success.
        self._emit_state_change()
        if self.state() == "open":
            raise UpstreamUnavailableError("circuit_breaker_open: upstream is failing fast")

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None
        self._emit_state_change()

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._opened_at = self._clock()
        self._emit_state_change()

    def _emit_state_change(self) -> None:
        current = self.state()
        if current != self._last_observed_state:
            self._last_observed_state = current
            if self._on_state_change is not None:
                self._on_state_change(current)
