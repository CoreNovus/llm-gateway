"""Settings for the gateway.

Single source of truth for runtime configuration. Read once at process boot
via :func:`get_settings`; never read environment variables anywhere else in
the package — keeps the test seam single-pointed.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Cloud-provider metadata services we refuse to forward traffic to —
# the gateway must never become an SSRF relay that lets a remote caller
# steal IAM credentials from the host's IMDS endpoint. Keep the set
# explicit (not a generic link-local check) so legitimate private-LAN
# upstreams in 10.0.0.0/8 / 172.16.0.0/12 / 192.168.0.0/16 stay allowed.
_BLOCKED_UPSTREAM_NETWORKS: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("169.254.169.254/32"),  # AWS / GCP / Azure IMDS
    ipaddress.IPv4Network("169.254.170.2/32"),  # ECS task metadata v2
    ipaddress.IPv4Network("169.254.170.23/32"),  # ECS task metadata v4
    ipaddress.IPv4Network("100.100.100.200/32"),  # Alibaba Cloud metadata
)


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables.

    Field names use the ``vllm_*`` prefix where they parallel a setting
    a consuming client may also set, so a single ``.env`` file can
    drive both ends during local development.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Server bind ─────────────────────────────────────────────────────
    server_host: str = Field(
        default="127.0.0.1",
        description=(
            "Bind address. Default 127.0.0.1 — the SSH tunnel from the "
            "operator's laptop is the auth boundary. NEVER set to 0.0.0.0 "
            "in production."
        ),
    )
    server_port: int = Field(default=8000, ge=1, le=65535)

    # ── vLLM upstream ──────────────────────────────────────────────────
    vllm_upstream_url: str = Field(
        default="http://127.0.0.1:18000",
        description=(
            "URL of the vendor vLLM Docker container. The HTTP proxy "
            "forwards /v1/chat/completions and /v1/models here. Must "
            "be an http(s) URL; cloud metadata-service IPs are rejected "
            "to keep the gateway from being weaponised as an SSRF relay."
        ),
    )

    @field_validator("vllm_upstream_url")
    @classmethod
    def _reject_metadata_service_upstreams(cls, value: str) -> str:
        """Refuse upstream URLs that target cloud metadata endpoints.

        The gateway forwards inbound POST bodies to whatever this URL
        points at. If an operator misconfigures it to ``169.254.169.254``
        (or any other documented metadata IP) the gateway becomes a
        credential-relay reachable through any public-facing entry
        point. We require an ``http``/``https`` URL with a parseable
        host, and we reject hosts whose literal IP matches a known
        metadata-service endpoint. Hostnames that resolve to those IPs
        are NOT looked up here — DNS rebinding requires the upstream
        client to take its own defence; the static check stops the
        most common operator footgun.
        """
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(
                "vllm_upstream_url must use http or https — got " f"scheme={parsed.scheme!r}."
            )
        if not parsed.hostname:
            raise ValueError("vllm_upstream_url must include a host.")
        try:
            ip = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            return value  # hostname (not literal IP) — accept
        if not isinstance(ip, ipaddress.IPv4Address):
            return value
        for network in _BLOCKED_UPSTREAM_NETWORKS:
            if ip in network:
                raise ValueError(
                    "vllm_upstream_url targets a cloud metadata-service "
                    f"address ({ip}); refusing to forward traffic to it."
                )
        return value

    vllm_request_timeout_s: float = Field(
        default=600.0,
        gt=0,
        description=(
            "Per-request timeout against vLLM. Generous — non-streaming "
            "completions on a cold-loaded model can take minutes."
        ),
    )

    # ── Defaults ────────────────────────────────────────────────────────
    default_model_id: str = Field(
        default="selfhost-qwen",
        description=(
            "Served-name shown in /v1/models when no model is specified. "
            "Must be a key in models.registry.DEFAULT_REGISTRY."
        ),
    )

    # ── Auth ────────────────────────────────────────────────────────────
    bearer_token: str = Field(
        default="",
        description=(
            "Bearer token required by the auth middleware. Empty disables "
            "auth — only acceptable for local dev with the NoopBackend; "
            "the production CDK stack refuses to deploy without a "
            "populated Secrets Manager value."
        ),
    )

    # ── Rate limit ──────────────────────────────────────────────────────
    rate_limit_rpm: int = Field(
        default=60,
        gt=0,
        description=(
            "Per-key requests-per-minute cap. Token-bucket refills "
            "rpm/60 tokens per second, capped at rpm tokens."
        ),
    )
    rate_limit_max_keys: int = Field(
        default=10_000,
        gt=0,
        description=(
            "Maximum unique keys held in the in-memory token bucket. "
            "Caps memory under an attacker churning fake bearer tokens; "
            "LRU evicts the oldest entry on overflow."
        ),
    )
    rate_limit_idle_evict_s: float = Field(
        default=3600.0,
        gt=0,
        description=(
            "Drop bucket entries whose last hit is older than this "
            "many seconds. Lazy sweep on every check() call."
        ),
    )

    # ── Body-size cap ───────────────────────────────────────────────────
    max_request_body_bytes: int = Field(
        default=1_048_576,  # 1 MiB
        gt=0,
        description=(
            "Reject requests whose Content-Length exceeds this many "
            "bytes with 413. Defaults to 1 MiB — large enough for any "
            "reasonable agent prompt + tool schema, small enough to "
            "stop a hostile caller from blowing the process before "
            "pydantic ever sees the body."
        ),
    )

    # ── Circuit breaker ─────────────────────────────────────────────────
    circuit_breaker_enabled: bool = Field(
        default=True,
        description=(
            "Wrap the inference backend with a circuit breaker that opens "
            "after consecutive upstream failures. Disable for local dev "
            "with NoopBackend if it gets in the way of debugging."
        ),
    )
    circuit_breaker_failure_threshold: int = Field(default=5, gt=0)
    circuit_breaker_cooldown_s: float = Field(default=30.0, gt=0)

    # ── Logging ─────────────────────────────────────────────────────────
    log_level: str = Field(default="INFO")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` singleton.

    Cached so the env is read once. Tests that need a different
    configuration construct ``Settings(...)`` directly and pass it to
    :func:`llm_gateway.app.create_app`.
    """
    return Settings()
