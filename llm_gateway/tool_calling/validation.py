"""Pre-flight validation for tool-using chat-completion requests.

Single responsibility: given an inbound request body and the resolved
:class:`ModelDefinition`, decide whether the tool combination is safe
to forward to vLLM. Returns a :class:`ToolRequestRejection` (carrying
the API-layer error envelope fields) if not, ``None`` if so.

This module is **not** the place for response validation, retry-after
logic, or HTTP concerns — it purely answers "should we forward?".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from llm_gateway.tool_calling.parsers import ToolCallParser

if TYPE_CHECKING:
    # Type-only import — runtime import would close a cycle through
    # ``models.registry`` (which imports :class:`ToolCallParser`).
    from llm_gateway.models.registry import ModelDefinition


@dataclass(frozen=True)
class ToolRequestRejection:
    """Carries the reason a tool request was refused pre-flight.

    Field shape matches what the API layer's error envelope wants
    (``code`` + ``message``) so the endpoint can lift them in one line.
    """

    code: str
    message: str


def validate_tool_request(
    request: dict[str, Any],
    model: ModelDefinition,
) -> ToolRequestRejection | None:
    """Refuse a tool-using request whose model cannot serve it.

    Returns ``None`` (request is safe to forward) when:

    * the request carries no ``tools`` field, or
    * the request carries tools AND the model's parser is not
      :class:`ToolCallParser.NONE`.

    Returns a :class:`ToolRequestRejection` otherwise. Future checks
    for tool-schema validity, per-model tool count caps, and
    ``tool_choice`` consistency belong here — keep each new check as
    a separate predicate so the rule set stays auditable.
    """
    tools = request.get("tools")
    if not tools:
        return None

    if model.tool_call_parser == ToolCallParser.NONE:
        return ToolRequestRejection(
            code="model_does_not_support_tools",
            message=(
                f"Model {model.served_name!r} cannot serve tool-using "
                f"requests (tool_call_parser=none). Pick a model whose "
                f"tool_call_parser is one of: hermes, llama3_json, mistral."
            ),
        )
    return None
