"""Tool-call parser registry — typed view of the vLLM ``--tool-call-parser`` flag.

vLLM does the actual tool-call parsing on the engine side: when a model
emits its native tool-call shape (Qwen's ``<tool_call>`` blocks,
Llama-3's JSON tool block, …) vLLM's parser converts it to the OpenAI
``tool_calls`` array. We don't reimplement that work — we just keep a
typed enum of the parsers we have validated against the model registry,
so:

* a typo in :data:`DEFAULT_REGISTRY` fails at import time, not in
  production at first tool call;
* the pre-flight validator can refuse tool requests against models
  whose parser is :data:`ToolCallParser.NONE`.

When a new model lands, add it to the model registry referencing one of
these enum members. If a *new family* of model is supported (e.g. a
hypothetical ``functionary`` parser), add the enum member here first
and pin the supported vLLM version in the docstring — the operator
runbook reads off this enum.
"""

from __future__ import annotations

from enum import Enum


class ToolCallParser(str, Enum):
    """vLLM tool-call parsers we have validated against a model.

    Values match the literal expected by ``vllm`` ``--tool-call-parser``;
    that lets the operator runbook print the enum value into a
    docker-compose / systemd unit without translation.
    """

    HERMES = "hermes"
    """Qwen 2.5+ (and other Hermes-style ChatML tool blocks)."""

    LLAMA3_JSON = "llama3_json"
    """Llama-3.1+ Instruct family — JSON tool blocks."""

    MISTRAL = "mistral"
    """Mistral-Instruct family."""

    NONE = "none"
    """Model does not support tool calls — pre-flight refuses tool requests."""
