"""Model registry tests."""

from __future__ import annotations

import pytest

from llm_gateway.models.registry import (
    DEFAULT_REGISTRY,
    ModelDefinition,
    lookup_by_served_name,
)

# ─── LOGIC: default entry shape ─────────────────────────────────────────────


def test_default_registry_contains_selfhost_qwen() -> None:
    assert "selfhost-qwen" in DEFAULT_REGISTRY


def test_default_qwen_entry_matches_documented_invariants() -> None:
    """The default Qwen entry is anchored on Qwen2.5-7B-Instruct-AWQ +
    the hermes tool-call parser + 8192 max-model-len. A drift here
    would silently change the contract operators rely on."""
    qwen = DEFAULT_REGISTRY["selfhost-qwen"]
    assert qwen.hf_repo == "Qwen/Qwen2.5-7B-Instruct-AWQ"
    assert qwen.tool_call_parser == "hermes"
    assert qwen.max_model_len == 8192


# ─── LOGIC: lookup happy path + unknown ─────────────────────────────────────


def test_lookup_by_served_name_returns_definition() -> None:
    result = lookup_by_served_name("selfhost-qwen")
    assert isinstance(result, ModelDefinition)
    assert result.served_name == "selfhost-qwen"


def test_lookup_unknown_name_returns_none() -> None:
    assert lookup_by_served_name("does-not-exist") is None


# ─── ERROR: registry is read-only ───────────────────────────────────────────


def test_registry_rejects_mutation() -> None:
    """MappingProxyType protects against in-place edits at runtime — a
    future bug cannot register a model by writing to DEFAULT_REGISTRY."""
    with pytest.raises(TypeError):
        DEFAULT_REGISTRY["new-model"] = ModelDefinition(  # type: ignore[index]
            served_name="new-model",
            hf_repo="x/y",
            tool_call_parser="hermes",
            max_model_len=4096,
            recommended_vram_gb=8,
        )
