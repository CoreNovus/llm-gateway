"""Model registry — metadata only, never weights."""

from llm_gateway.models.registry import (
    DEFAULT_REGISTRY,
    ModelDefinition,
    lookup_by_served_name,
)

__all__ = ["DEFAULT_REGISTRY", "ModelDefinition", "lookup_by_served_name"]
