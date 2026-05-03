"""Rate-limit middleware + InMemoryTokenBucket tests.

Tests separate the strategy (``InMemoryTokenBucket``) from the
middleware integration so failures point at one concern at a time.
The bucket gets a fake clock for deterministic refill timing.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway.middleware.rate_limit import (
    InMemoryTokenBucket,
    RateLimitDecision,
    RateLimitMiddleware,
)

# ─── InMemoryTokenBucket — strategy ─────────────────────────────────────────


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_bucket_rejects_zero_or_negative_rpm() -> None:
    with pytest.raises(ValueError, match="rpm"):
        InMemoryTokenBucket(rpm=0)


@pytest.mark.asyncio
async def test_bucket_allows_first_call_at_full_capacity() -> None:
    bucket = InMemoryTokenBucket(rpm=60, clock=_FakeClock())
    decision = await bucket.check("alice")
    assert decision.allowed is True
    assert decision.retry_after_s == 0


@pytest.mark.asyncio
async def test_bucket_rejects_when_drained_and_returns_retry_after() -> None:
    clock = _FakeClock()
    bucket = InMemoryTokenBucket(rpm=2, clock=clock)  # 2 rpm = 1 token / 30s
    # Drain capacity (2 tokens).
    await bucket.check("alice")
    await bucket.check("alice")
    decision = await bucket.check("alice")
    assert decision.allowed is False
    assert decision.retry_after_s >= 1


@pytest.mark.asyncio
async def test_bucket_refills_over_time() -> None:
    clock = _FakeClock()
    bucket = InMemoryTokenBucket(rpm=60, clock=clock)  # 1 token / s
    # Drain capacity.
    for _ in range(60):
        await bucket.check("alice")
    assert (await bucket.check("alice")).allowed is False

    # Advance 2s — should regain 2 tokens.
    clock.advance(2.0)
    assert (await bucket.check("alice")).allowed is True
    assert (await bucket.check("alice")).allowed is True
    # Third call still drained.
    assert (await bucket.check("alice")).allowed is False


@pytest.mark.asyncio
async def test_bucket_keys_are_independent() -> None:
    bucket = InMemoryTokenBucket(rpm=1, clock=_FakeClock())
    assert (await bucket.check("alice")).allowed is True
    # Bob's bucket is independent.
    assert (await bucket.check("bob")).allowed is True
    # Alice's already drained.
    assert (await bucket.check("alice")).allowed is False


# ─── bounded memory — LRU cap + TTL eviction ───────────────────────────────


def test_zero_max_keys_rejected() -> None:
    with pytest.raises(ValueError, match="max_keys"):
        InMemoryTokenBucket(rpm=60, max_keys=0)


def test_zero_idle_evict_s_rejected() -> None:
    with pytest.raises(ValueError, match="idle_evict_s"):
        InMemoryTokenBucket(rpm=60, idle_evict_s=0)


@pytest.mark.asyncio
async def test_bucket_lru_evicts_oldest_on_overflow() -> None:
    """At max_keys=2, adding a third key drops the LRU entry."""
    bucket = InMemoryTokenBucket(rpm=10, max_keys=2, clock=_FakeClock())
    await bucket.check("alice")  # MRU=alice
    await bucket.check("bob")  # MRU=bob, LRU=alice
    await bucket.check("carol")  # alice evicted; MRU=carol, LRU=bob

    # alice's state was dropped — her next check sees a fresh bucket
    # (full capacity, allowed=True).
    decision = await bucket.check("alice")
    assert decision.allowed is True

    # And the dict still has at most max_keys entries.
    assert len(bucket._buckets) <= 2  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_bucket_idle_eviction_drops_stale_entries() -> None:
    """Entries untouched for longer than idle_evict_s are dropped on
    the next check, even when max_keys hasn't been hit."""
    clock = _FakeClock()
    bucket = InMemoryTokenBucket(rpm=10, idle_evict_s=60, clock=clock)
    await bucket.check("alice")
    assert "alice" in bucket._buckets  # type: ignore[attr-defined]

    # Advance past idle_evict_s; sweep happens on the next check.
    clock.advance(120)
    await bucket.check("bob")  # any check triggers the sweep
    assert "alice" not in bucket._buckets  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_bucket_idle_eviction_skips_recent_entries() -> None:
    """Recent entries survive the sweep — the cap on sweep budget +
    early-stop on first non-stale keeps the sweep O(1)."""
    clock = _FakeClock()
    bucket = InMemoryTokenBucket(rpm=10, idle_evict_s=60, clock=clock)
    await bucket.check("alice")
    clock.advance(10)  # well within idle window
    await bucket.check("bob")

    assert "alice" in bucket._buckets  # type: ignore[attr-defined]
    assert "bob" in bucket._buckets  # type: ignore[attr-defined]


# ─── RateLimitMiddleware — integration ──────────────────────────────────────


class _StubLimiter:
    """Always-deny / always-allow stub for middleware integration tests."""

    def __init__(self, *, allow: bool, retry_after_s: int = 0) -> None:
        self._allow = allow
        self._retry = retry_after_s
        self.calls: list[str] = []

    async def check(self, key: str) -> RateLimitDecision:
        self.calls.append(key)
        return RateLimitDecision(allowed=self._allow, retry_after_s=self._retry)


def _client(limiter: _StubLimiter) -> TestClient:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, limiter=limiter)

    @app.get("/v1/anything")
    def anything() -> dict[str, str]:
        return {"ok": "true"}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return TestClient(app)


def test_middleware_passes_request_when_limiter_allows() -> None:
    import hashlib

    limiter = _StubLimiter(allow=True)
    response = _client(limiter).get("/v1/anything", headers={"Authorization": "Bearer t1"})
    assert response.status_code == 200
    # The bearer is hashed before becoming the bucket key — the
    # plaintext token must not appear in the bucket dict.
    # 32 hex chars (128 bits) — bumped from 16 to close a token-grinding
    # collision attack. See PR #6 / commit f40049e.
    expected_digest = hashlib.sha256(b"t1").hexdigest()[:32]
    assert limiter.calls == [f"token:{expected_digest}"]


def test_middleware_key_hash_does_not_contain_plaintext_token() -> None:
    """Regression guard — the rate-limit key must never carry the raw
    bearer. Future maintainers reaching for ``f"token:{value}"`` will
    fail this test loudly."""
    limiter = _StubLimiter(allow=True)
    sentinel = "placeholder-do-not-leak"
    _client(limiter).get("/v1/anything", headers={"Authorization": f"Bearer {sentinel}"})
    assert sentinel not in limiter.calls[0]


def test_middleware_returns_429_with_retry_after_when_denied() -> None:
    limiter = _StubLimiter(allow=False, retry_after_s=42)
    response = _client(limiter).get("/v1/anything")
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "42"
    assert response.json() == {"error": "rate_limit_exceeded"}


def test_middleware_skips_health_and_ready_paths() -> None:
    limiter = _StubLimiter(allow=False, retry_after_s=99)
    response = _client(limiter).get("/health")
    assert response.status_code == 200
    assert limiter.calls == []  # health bypass — no check made


def test_middleware_falls_back_to_ip_key_when_no_bearer() -> None:
    limiter = _StubLimiter(allow=True)
    _client(limiter).get("/v1/anything")
    assert len(limiter.calls) == 1
    assert limiter.calls[0].startswith("ip:")
