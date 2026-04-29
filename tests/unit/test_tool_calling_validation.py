"""``validate_tool_request`` pre-flight tests."""

from __future__ import annotations

from typing import Any

from llm_gateway.models.registry import ModelDefinition
from llm_gateway.tool_calling.parsers import ToolCallParser
from llm_gateway.tool_calling.validation import (
    ToolRequestRejection,
    validate_tool_request,
)


def _model(parser: ToolCallParser) -> ModelDefinition:
    return ModelDefinition(
        served_name="test-model",
        hf_repo="test/repo",
        tool_call_parser=parser,
        max_model_len=4096,
        recommended_vram_gb=8,
    )


def _request(*, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hi"}],
    }
    if tools is not None:
        body["tools"] = tools
    return body


# ─── LOGIC: no tools means no rejection ────────────────────────────────────


def test_request_without_tools_is_always_allowed() -> None:
    for parser in ToolCallParser:
        assert validate_tool_request(_request(), _model(parser)) is None


def test_empty_tools_list_is_treated_as_no_tools() -> None:
    """An empty ``tools=[]`` is what some clients send when they have no
    tools to advertise; treating it as 'tools requested' would surprise
    them with a 400."""
    request = _request(tools=[])
    for parser in ToolCallParser:
        assert validate_tool_request(request, _model(parser)) is None


# ─── LOGIC: tool-supporting parsers pass through ───────────────────────────


def test_tool_request_passes_when_model_has_a_real_parser() -> None:
    request = _request(tools=[{"type": "function", "function": {"name": "x"}}])
    for parser in (
        ToolCallParser.HERMES,
        ToolCallParser.LLAMA3_JSON,
        ToolCallParser.MISTRAL,
    ):
        assert validate_tool_request(request, _model(parser)) is None


# ─── ERROR: tools against parser=NONE is refused ───────────────────────────


def test_tool_request_against_none_parser_returns_rejection() -> None:
    request = _request(tools=[{"type": "function", "function": {"name": "x"}}])
    rejection = validate_tool_request(request, _model(ToolCallParser.NONE))

    assert isinstance(rejection, ToolRequestRejection)
    assert rejection.code == "model_does_not_support_tools"
    assert "test-model" in rejection.message
