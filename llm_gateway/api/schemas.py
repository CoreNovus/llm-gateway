"""OpenAI-compatible request schemas for the API layer.

We Pydantic-validate the *request* shape so we can make routing
decisions (model lookup, ``stream`` gating) and reject obvious garbage
before forwarding to vLLM. We deliberately do NOT model the
*response* — vLLM is the source of truth for response shape, and
modeling it would bind us to a specific OpenAI version.

``ConfigDict(extra="allow")`` so vLLM-specific fields (e.g.
``logprobs``, ``frequency_penalty``, ``response_format``) flow through
without us having to know about every one. Keeps the schema OCP — new
OpenAI fields don't need a code change.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    """OpenAI chat message — role + content + optional tool fields."""

    model_config = ConfigDict(extra="allow")

    role: str
    # ``content`` is ``str`` for plain text and ``list[dict]`` for the
    # multimodal / structured-content shape. We stay agnostic.
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ChatCompletionRequest(BaseModel):
    """OpenAI ``/v1/chat/completions`` request body."""

    model_config = ConfigDict(extra="allow")

    model: str = Field(..., min_length=1)
    messages: list[ChatMessage] = Field(..., min_length=1)
    max_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    stop: str | list[str] | None = None
    n: int | None = Field(default=None, ge=1)
    seed: int | None = None
