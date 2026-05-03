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

Caps below complement the request-body byte cap in
``middleware/body_limit.py``: the byte cap is a coarse safety net
(1 MiB) measured at the transport layer; the field-level caps reject
obviously-abusive payloads with a precise pydantic error before vLLM
spends multi-second CPU rendering tens of thousands of tools or
messages into the prompt template.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Caps tuned to be generous for any plausible legitimate workload while
# still rejecting payloads that turn into a vLLM template-render DoS.
# A 1 MiB body holds either ~5K minimum-size tools (~200 B each) or
# ~100K empty-string messages — well above the caps below, so the body
# limit catches the truly catastrophic cases and these caps catch the
# "fits in a body but is still abusive" cases with a clear 422.
_MAX_MESSAGES_PER_REQUEST = 1024
_MAX_TOOLS_PER_REQUEST = 64
_MAX_CONTENT_CHARS = 100_000  # ~25 K tokens — beyond any single model's window today


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

    @field_validator("content")
    @classmethod
    def _content_length_cap(cls, value: str | list[dict[str, Any]] | None) -> Any:
        """Reject string content longer than the per-message cap.

        Multimodal list-of-dicts content is not bounded here; the body
        size cap is the right granularity for "many parts" attacks
        because each part is its own dict whose fields are vLLM's
        responsibility to validate.
        """
        if isinstance(value, str) and len(value) > _MAX_CONTENT_CHARS:
            raise ValueError(f"content exceeds {_MAX_CONTENT_CHARS} chars (got {len(value)})")
        return value


class ChatCompletionRequest(BaseModel):
    """OpenAI ``/v1/chat/completions`` request body."""

    model_config = ConfigDict(extra="allow")

    model: str = Field(..., min_length=1)
    messages: list[ChatMessage] = Field(..., min_length=1, max_length=_MAX_MESSAGES_PER_REQUEST)
    max_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    stream: bool = False
    tools: list[dict[str, Any]] | None = Field(default=None, max_length=_MAX_TOOLS_PER_REQUEST)
    tool_choice: str | dict[str, Any] | None = None
    stop: str | list[str] | None = None
    n: int | None = Field(default=None, ge=1)
    seed: int | None = None
