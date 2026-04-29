"""``CircuitBreakerBackend`` tests — state-machine + decorator semantics.

The breaker uses an injected ``clock`` for deterministic transitions —
tests advance simulated time without ``asyncio.sleep`` or wall clock.
The inner backend is a hand-rolled stub that raises the right
exception class for the test case; we don't need ``NoopBackend`` here.
"""

from __future__ import annotations

from typing import Any

import pytest

from llm_gateway.inference.circuit_breaker import CircuitBreakerBackend
from llm_gateway.inference.errors import UpstreamClientError, UpstreamUnavailableError


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FailingBackend:
    """Inner backend whose ``complete`` always raises Unavailable."""

    aclose_called = False

    async def aclose(self) -> None:
        type(self).aclose_called = True

    async def ping(self) -> bool:
        return False

    async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        raise UpstreamUnavailableError("upstream down")

    async def stream(self, request: dict[str, Any]) -> Any:
        raise UpstreamUnavailableError("upstream down")


class _SuccessBackend:
    async def aclose(self) -> None:
        pass

    async def ping(self) -> bool:
        return True

    async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    async def stream(self, request: dict[str, Any]) -> Any:
        async def _gen() -> Any:
            yield b"chunk"

        return _gen()


# ─── ERROR: configuration validation ───────────────────────────────────────


def test_zero_failure_threshold_rejected() -> None:
    with pytest.raises(ValueError, match="failure_threshold"):
        CircuitBreakerBackend(_SuccessBackend(), failure_threshold=0)


def test_zero_cooldown_rejected() -> None:
    with pytest.raises(ValueError, match="cooldown"):
        CircuitBreakerBackend(_SuccessBackend(), cooldown_s=0)


# ─── LOGIC: closed → open after threshold ──────────────────────────────────


@pytest.mark.asyncio
async def test_breaker_opens_after_threshold_consecutive_failures() -> None:
    clock = _FakeClock()
    breaker = CircuitBreakerBackend(
        _FailingBackend(), failure_threshold=3, cooldown_s=10, clock=clock
    )
    assert breaker.state() == "closed"

    for _ in range(3):
        with pytest.raises(UpstreamUnavailableError):
            await breaker.complete({})

    assert breaker.state() == "open"


@pytest.mark.asyncio
async def test_breaker_open_state_short_circuits_without_calling_inner() -> None:
    """While open, requests must NOT touch the inner backend — that's the
    whole point of fail-fast."""
    clock = _FakeClock()
    inner = _FailingBackend()
    breaker = CircuitBreakerBackend(inner, failure_threshold=1, cooldown_s=30, clock=clock)
    # First call opens the breaker.
    with pytest.raises(UpstreamUnavailableError):
        await breaker.complete({})

    # Replace inner.complete with a counter — confirm it's not called.
    call_count = 0

    async def _spy(request: dict[str, Any]) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        return {"ok": True}

    inner.complete = _spy  # type: ignore[method-assign]

    with pytest.raises(UpstreamUnavailableError, match="circuit_breaker_open"):
        await breaker.complete({})
    assert call_count == 0


# ─── LOGIC: half-open after cooldown ───────────────────────────────────────


@pytest.mark.asyncio
async def test_breaker_enters_half_open_after_cooldown() -> None:
    clock = _FakeClock()
    breaker = CircuitBreakerBackend(
        _FailingBackend(), failure_threshold=1, cooldown_s=10, clock=clock
    )
    with pytest.raises(UpstreamUnavailableError):
        await breaker.complete({})
    assert breaker.state() == "open"

    clock.advance(10.5)
    assert breaker.state() == "half-open"


@pytest.mark.asyncio
async def test_half_open_success_closes_breaker() -> None:
    clock = _FakeClock()
    inner = _FailingBackend()
    breaker = CircuitBreakerBackend(inner, failure_threshold=1, cooldown_s=10, clock=clock)
    with pytest.raises(UpstreamUnavailableError):
        await breaker.complete({})

    # Cooldown elapses; swap inner to a success backend (simulates
    # vLLM coming back). Half-open probe succeeds → closed.
    clock.advance(11)

    async def _ok(request: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    inner.complete = _ok  # type: ignore[method-assign]

    result = await breaker.complete({})
    assert result == {"ok": True}
    assert breaker.state() == "closed"


@pytest.mark.asyncio
async def test_half_open_failure_reopens_breaker() -> None:
    clock = _FakeClock()
    breaker = CircuitBreakerBackend(
        _FailingBackend(), failure_threshold=1, cooldown_s=10, clock=clock
    )
    with pytest.raises(UpstreamUnavailableError):
        await breaker.complete({})
    clock.advance(11)
    assert breaker.state() == "half-open"

    with pytest.raises(UpstreamUnavailableError):
        await breaker.complete({})
    assert breaker.state() == "open"


# ─── LOGIC: ClientError doesn't break the circuit ──────────────────────────


@pytest.mark.asyncio
async def test_client_errors_do_not_trip_the_breaker() -> None:
    """4xx is the caller's fault; do not penalise the engine."""

    class _ClientErrorBackend:
        async def aclose(self) -> None:
            pass

        async def ping(self) -> bool:
            return True

        async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
            raise UpstreamClientError(status_code=400, body={"error": "bad"})

        async def stream(self, request: dict[str, Any]) -> Any:
            raise UpstreamClientError(status_code=400, body={"error": "bad"})

    breaker = CircuitBreakerBackend(_ClientErrorBackend(), failure_threshold=2, cooldown_s=10)
    for _ in range(5):
        with pytest.raises(UpstreamClientError):
            await breaker.complete({})

    assert breaker.state() == "closed"


# ─── LOGIC: ping always probes ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ping_does_not_short_circuit_when_open() -> None:
    """Readiness must reflect actual reachability, not breaker state."""
    clock = _FakeClock()
    breaker = CircuitBreakerBackend(
        _FailingBackend(), failure_threshold=1, cooldown_s=30, clock=clock
    )
    with pytest.raises(UpstreamUnavailableError):
        await breaker.complete({})
    assert breaker.state() == "open"

    # ping reaches the (failing) inner — still returns False, but the
    # call goes through; the breaker doesn't intercept.
    assert await breaker.ping() is False


# ─── OBJECT-STATE: aclose forwards to inner ────────────────────────────────


@pytest.mark.asyncio
async def test_aclose_forwards_to_inner_backend() -> None:
    inner = _FailingBackend()
    type(inner).aclose_called = False
    breaker = CircuitBreakerBackend(inner, failure_threshold=5, cooldown_s=10)

    await breaker.aclose()

    assert type(inner).aclose_called is True
