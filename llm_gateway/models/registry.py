"""Open-source model registry — metadata only.

Weights are NOT in this repo and never will be. This module owns the
small piece of operator knowledge that travels with the code: which
HuggingFace repo a served-name maps to, which tool-call parser the
model expects, and the recommended VRAM ceiling so we can refuse to
serve a model that cannot fit on the host GPU.

Adding a model = one new ``ModelDefinition`` literal added to
:data:`DEFAULT_REGISTRY`. Existing entries do not change; consumers
look up by ``served_name`` so caller sites are unaffected.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from llm_gateway.tool_calling.parsers import ToolCallParser


@dataclass(frozen=True)
class ModelDefinition:
    """Static facts about an open-source model we know how to serve.

    Attributes:
        served_name: Stable id the gateway exposes via ``/v1/models``
            and accepts in the ``model`` field of requests. Decoupled
            from the upstream HuggingFace path so we can swap the
            actual weights (e.g. a re-quantised checkpoint) without
            breaking client config.
        hf_repo: HuggingFace repo to pull weights from (used by the
            operator-side ``load-model.sh``; this package never
            downloads).
        tool_call_parser: vLLM's ``--tool-call-parser`` value matching
            the model family. See :class:`ToolCallParser` for the
            enumerated set; ``ToolCallParser.NONE`` flags a model that
            does not support tool calls and triggers a pre-flight
            refusal in :mod:`llm_gateway.tool_calling.validation`.
        max_model_len: Default ``--max-model-len`` for vLLM. Includes
            prompt + output; KV-cache headroom is negotiated by the
            engine.
        recommended_vram_gb: Soft cap on host VRAM. We refuse to start
            a model whose recommendation exceeds the available GPU.
    """

    served_name: str
    hf_repo: str
    tool_call_parser: ToolCallParser
    max_model_len: int
    recommended_vram_gb: int


_QWEN_2_5_7B_INSTRUCT_AWQ = ModelDefinition(
    served_name="selfhost-qwen",
    hf_repo="Qwen/Qwen2.5-7B-Instruct-AWQ",
    tool_call_parser=ToolCallParser.HERMES,
    max_model_len=8192,
    recommended_vram_gb=12,
)


# Read-only at runtime so a future bug cannot mutate the registry. Adding
# a new model = a new ModelDefinition literal + a new entry below.
DEFAULT_REGISTRY: Mapping[str, ModelDefinition] = MappingProxyType(
    {
        _QWEN_2_5_7B_INSTRUCT_AWQ.served_name: _QWEN_2_5_7B_INSTRUCT_AWQ,
    }
)


def lookup_by_served_name(name: str) -> ModelDefinition | None:
    """Return the registered model for ``name`` or ``None`` if unknown.

    ``None`` rather than raising — callers (the ``/v1/chat/completions``
    handler and the ``/v1/models`` lister) decide the correct error
    shape for their context.
    """
    return DEFAULT_REGISTRY.get(name)
