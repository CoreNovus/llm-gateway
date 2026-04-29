"""``__main__.main`` startup-guard tests."""

from __future__ import annotations

import pytest

from llm_gateway.__main__ import _require_bearer_token


def test_require_bearer_token_raises_when_token_is_empty() -> None:
    with pytest.raises(RuntimeError, match="BEARER_TOKEN must be set"):
        _require_bearer_token("")


def test_require_bearer_token_passes_when_token_is_set() -> None:
    # Non-empty value satisfies the guard — content not validated here.
    _require_bearer_token("any-non-empty-value")
