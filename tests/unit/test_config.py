"""Settings tests — defaults, env overrides, security guard.

Four-category coverage: LOGIC (env override), BOUNDARY (defaults),
ERROR (invalid port), OBJECT-STATE (singleton cache).
"""

from __future__ import annotations

import pytest

from llm_gateway.config import Settings, get_settings

# ─── BOUNDARY: defaults ─────────────────────────────────────────────────────


def test_settings_defaults_match_local_dev_topology() -> None:
    settings = Settings()
    assert settings.server_host == "127.0.0.1"
    assert settings.server_port == 8000
    assert settings.vllm_upstream_url == "http://127.0.0.1:18000"
    assert settings.default_model_id == "selfhost-qwen"
    assert settings.bearer_token == ""
    assert settings.log_level == "INFO"


# ─── LOGIC: env override + case insensitivity ───────────────────────────────


def test_env_override_picks_up_lowercase_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("vllm_upstream_url", "http://example.test:9999")
    assert Settings().vllm_upstream_url == "http://example.test:9999"


def test_env_override_picks_up_uppercase_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVER_PORT", "9100")
    assert Settings().server_port == 9100


# ─── ERROR: invalid port ────────────────────────────────────────────────────


def test_invalid_server_port_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVER_PORT", "70000")  # > 65535
    with pytest.raises(ValueError):
        Settings()


# ─── ERROR: vllm_upstream_url SSRF guard ────────────────────────────────────


@pytest.mark.parametrize(
    "metadata_url",
    [
        "http://169.254.169.254/",  # AWS / GCP / Azure IMDS
        "http://169.254.169.254:8080/",  # IMDS with custom port
        "http://169.254.170.2/",  # ECS task metadata v2
        "http://169.254.170.23/",  # ECS task metadata v4
        "http://100.100.100.200/",  # Alibaba metadata
    ],
)
def test_metadata_service_upstream_is_rejected(metadata_url: str) -> None:
    """An operator who points the upstream at a cloud metadata IP would
    weaponise the gateway as an SSRF / credential-relay. Reject at config
    load so misconfigurations fail loudly at boot, not silently at the
    first credential exfiltration."""
    with pytest.raises(ValueError, match="metadata-service"):
        Settings(vllm_upstream_url=metadata_url)


def test_loopback_upstream_is_allowed() -> None:
    """The default deployment topology has the gateway and vLLM on the
    same host bound to 127.0.0.1 — must remain allowed."""
    Settings(vllm_upstream_url="http://127.0.0.1:18000")  # no raise


def test_private_lan_upstream_is_allowed() -> None:
    """Legitimate private-LAN deployments (10/8, 172.16/12, 192.168/16)
    are NOT blocked — only documented metadata IPs are refused."""
    for url in (
        "http://10.0.0.5:8000",
        "http://172.16.0.1:8000",
        "http://192.168.1.10:8000",
    ):
        Settings(vllm_upstream_url=url)  # no raise


def test_hostname_upstream_is_allowed() -> None:
    """Docker bridge hostnames like ``http://vllm:8000`` resolve via
    DNS at request time — the static literal-IP check accepts them."""
    Settings(vllm_upstream_url="http://vllm:8000")  # no raise


def test_non_http_scheme_is_rejected() -> None:
    """Only http and https are valid for the upstream proxy."""
    with pytest.raises(ValueError, match="http or https"):
        Settings(vllm_upstream_url="file:///etc/passwd")


def test_url_without_host_is_rejected() -> None:
    with pytest.raises(ValueError, match="must include a host"):
        Settings(vllm_upstream_url="http:///")


# ─── OBJECT-STATE: singleton cache ─────────────────────────────────────────


def test_get_settings_returns_same_instance_on_repeat_calls() -> None:
    get_settings.cache_clear()
    a = get_settings()
    b = get_settings()
    assert a is b
