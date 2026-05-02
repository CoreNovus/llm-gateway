"""Per-key token-bucket rate limiting.

In-memory token bucket: refills ``rpm / 60`` tokens per second, caps at
``rpm`` tokens, drains by 1 per allowed request. 429 responses carry a
``Retry-After`` header (seconds) per RFC 7231.

The ``RateLimiter`` Protocol keeps the strategy swappable — a future
Redis-backed implementation lands without touching middleware code.
Today the only implementation is in-memory, sufficient while the
gateway runs on a single host.

Skip paths: ``/health`` and ``/ready`` always bypass rate limiting so
debug curls don't accidentally trip the bucket.
"""

import hashlib
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp


@dataclass(frozen=True)
class RateLimitDecision:
    """Outcome of a single rate-limit check."""

    allowed: bool
    retry_after_s: int  # 0 when allowed


class RateLimiter(Protocol):
    """Strategy protocol — implemented by ``InMemoryTokenBucket`` today."""

    async def check(self, key: str) -> RateLimitDecision: ...


class InMemoryTokenBucket:
    """Token bucket per key with bounded memory.

    Capacity == ``rpm`` tokens; refill rate == ``rpm / 60`` per second.
    A separate Redis-backed implementation is the natural slice if we
    ever scale beyond one gateway instance.

    Memory bounds (defence against attacker churning unique keys):
    * **LRU cap** — at most ``max_keys`` entries; on overflow the
      least-recently-touched entry is evicted via ``OrderedDict``.
    * **Lazy TTL eviction** — on each ``check()`` we sweep at most
      ``_TTL_SWEEP_BUDGET`` of the oldest entries; any whose
      ``last_refill_t`` is older than ``idle_evict_s`` are dropped.
      Keeps the dict trim under low traffic without the
      bookkeeping cost of a separate timer.

    A future Redis-backed implementation would honour the same
    ``RateLimiter`` Protocol — no caller change.
    """

    # How many oldest entries to inspect for TTL eviction per check.
    # Constant-time bound; sweep happens lazily on the call path.
    _TTL_SWEEP_BUDGET = 8

    def __init__(
        self,
        *,
        rpm: int,
        max_keys: int = 10_000,
        idle_evict_s: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rpm <= 0:
            raise ValueError("rpm must be positive")
        if max_keys <= 0:
            raise ValueError("max_keys must be positive")
        if idle_evict_s <= 0:
            raise ValueError("idle_evict_s must be positive")
        self._rate_per_s = rpm / 60.0
        self._capacity = float(rpm)
        self._max_keys = max_keys
        self._idle_evict_s = idle_evict_s
        # key → (tokens_remaining, last_refill_t). OrderedDict so we can
        # evict LRU on overflow and walk oldest-first for TTL sweep.
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._clock = clock

    async def check(self, key: str) -> RateLimitDecision:
        now = self._clock()
        self._evict_idle(now)

        existing = self._buckets.get(key)
        tokens, last_t = existing if existing is not None else (self._capacity, now)

        elapsed = max(0.0, now - last_t)
        tokens = min(self._capacity, tokens + elapsed * self._rate_per_s)

        if tokens >= 1.0:
            self._upsert(key, (tokens - 1.0, now))
            return RateLimitDecision(allowed=True, retry_after_s=0)

        # Caller must wait for (1 - tokens) more tokens at refill rate.
        wait_s = max(1, int((1.0 - tokens) / self._rate_per_s) + 1)
        self._upsert(key, (tokens, now))
        return RateLimitDecision(allowed=False, retry_after_s=wait_s)

    # ── internal helpers ────────────────────────────────────────────────

    def _upsert(self, key: str, value: tuple[float, float]) -> None:
        """Insert / refresh ``key`` and mark it as most-recently-used."""
        if key in self._buckets:
            self._buckets[key] = value
            self._buckets.move_to_end(key)
            return
        if len(self._buckets) >= self._max_keys:
            # Drop the least-recently-touched entry; bounded memory.
            self._buckets.popitem(last=False)
        self._buckets[key] = value

    def _evict_idle(self, now: float) -> None:
        """Drop up to ``_TTL_SWEEP_BUDGET`` oldest entries that have
        gone idle for longer than ``idle_evict_s``."""
        deadline = now - self._idle_evict_s
        # Walk oldest → newest; OrderedDict preserves insertion / move_to_end
        # order, so the first item is the LRU. ``evicted`` counts only the
        # branches that delete (not iteration index — early-return paths
        # don't bump it), so a plain counter is the right shape rather
        # than enumerate().
        evicted = 0
        for key in list(self._buckets):
            if evicted >= self._TTL_SWEEP_BUDGET:
                return
            _, last_t = self._buckets[key]
            if last_t > deadline:
                # First non-stale → all newer entries are also non-stale
                # (OrderedDict ordering is by recency), so we can stop.
                return
            del self._buckets[key]
            evicted += 1  # noqa: SIM113 — counter, not loop index


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Token-bucket rate-limit, keyed by bearer token (or client IP fallback)."""

    _DEFAULT_SKIP_PATHS: tuple[str, ...] = ("/metrics", "/health", "/ready")

    def __init__(
        self,
        app: ASGIApp,
        *,
        limiter: RateLimiter,
        skip_paths: Iterable[str] | None = None,
        on_rejection: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(app)
        self._limiter = limiter
        self._skip_paths = frozenset(skip_paths or self._DEFAULT_SKIP_PATHS)
        # Optional hook so the metrics layer can count 429s without
        # coupling this middleware to a Prometheus type.
        self._on_rejection = on_rejection

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path in self._skip_paths:
            return await call_next(request)

        decision = await self._limiter.check(self._key_from_request(request))
        if not decision.allowed:
            if self._on_rejection is not None:
                self._on_rejection()
            return JSONResponse(
                {"error": "rate_limit_exceeded"},
                status_code=429,
                headers={"Retry-After": str(decision.retry_after_s)},
            )
        return await call_next(request)

    @staticmethod
    def _key_from_request(request: Request) -> str:
        """Use a token-derived hash as the key; fall back to client IP.

        We hash the bearer with SHA-256 rather than storing the
        plaintext token in :class:`InMemoryTokenBucket._buckets`.
        That bucket dict otherwise lives in process memory keyed by
        the secret itself — a core dump or memory-inspection at a
        hostile time would leak every active token. Hashing is cheap
        (one SHA per request) and equally good as a partition key.

        With a single shared token the bucket effectively limits the
        whole gateway. With per-tenant tokens it automatically scopes
        per-tenant — no middleware change.

        The digest is truncated to 32 hex chars (128 bits). 64 bits
        hits a 50%-birthday-collision wall at ~4.3B unique tokens —
        far beyond any realistic deployment, but a token-cracker who
        knows the truncation could grind for a colliding token to
        share a quota bucket. 128 bits closes that grinding attack
        for the same memory cost in OrderedDict.
        """
        header = request.headers.get("authorization", "")
        prefix = "Bearer "
        if header.startswith(prefix):
            token = header[len(prefix) :].strip()
            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
            return f"token:{digest}"
        client = request.client
        return f"ip:{client.host if client else 'unknown'}"
