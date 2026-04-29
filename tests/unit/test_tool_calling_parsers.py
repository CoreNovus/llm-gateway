"""``ToolCallParser`` enum tests — value contract + registry alignment.

The enum's literal values are written into the operator runbook (and
into the docker-compose ``--tool-call-parser`` flag). A drift here
silently invalidates that runbook, so the test pins the exact strings.
"""

from __future__ import annotations

from llm_gateway.models.registry import DEFAULT_REGISTRY
from llm_gateway.tool_calling.parsers import ToolCallParser


def test_parser_values_match_vllm_cli_flag_literals() -> None:
    """vLLM ``--tool-call-parser`` accepts these exact strings."""
    assert ToolCallParser.HERMES.value == "hermes"
    assert ToolCallParser.LLAMA3_JSON.value == "llama3_json"
    assert ToolCallParser.MISTRAL.value == "mistral"
    assert ToolCallParser.NONE.value == "none"


def test_every_registry_entry_uses_a_known_parser() -> None:
    """A new model added to DEFAULT_REGISTRY must reference an enum
    member — no free-form strings — so a typo cannot ship to prod."""
    for defn in DEFAULT_REGISTRY.values():
        assert isinstance(defn.tool_call_parser, ToolCallParser)


def test_default_qwen_entry_uses_hermes_parser() -> None:
    """The Qwen 2.5 Instruct family emits Hermes-style ChatML tool
    blocks, so vLLM's ``hermes`` tool-call parser is the right match."""
    assert DEFAULT_REGISTRY["selfhost-qwen"].tool_call_parser is ToolCallParser.HERMES
