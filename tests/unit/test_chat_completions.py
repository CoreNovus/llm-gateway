"""``/v1/chat/completions`` endpoint tests — request validation, model
gating, error translation. The backend is the test-only ``NoopBackend``
or a hand-rolled stub that raises the inference exceptions; no socket
is opened.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

import pytest
from fastapi.testclient import TestClient

from llm_gateway.app import create_app
from llm_gateway.config import Settings
from llm_gateway.inference.errors import (
    UpstreamClientError,
    UpstreamUnavailableError,
)
from llm_gateway.inference.noop import NoopBackend
from llm_gateway.models.registry import DEFAULT_REGISTRY, ModelDefinition
from llm_gateway.tool_calling.parsers import ToolCallParser


def _client(backend: object) -> TestClient:
    # Bearer empty disables auth so tests focus on the endpoint behaviour.
    return TestClient(
        create_app(settings=Settings(bearer_token=""), backend=backend)  # type: ignore[arg-type]
    )


def _valid_request_body(model: str = "selfhost-qwen") -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
    }


# ─── LOGIC: happy-path passthrough ──────────────────────────────────────────


def test_valid_request_returns_backend_response_body() -> None:
    canned = {"id": "x", "object": "chat.completion", "choices": []}
    response = _client(NoopBackend(canned_response=canned)).post(
        "/v1/chat/completions", json=_valid_request_body()
    )
    assert response.status_code == 200
    assert response.json() == canned


def test_optional_fields_forwarded_to_backend() -> None:
    """Backend.complete should receive the original kwargs we accept
    (max_tokens, temperature, tool_choice) so vLLM honours them."""

    seen: dict[str, Any] = {}

    class _RecordingBackend:
        async def ping(self) -> bool:
            return True

        async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
            seen.update(request)
            return {"ok": True}

    body = {
        **_valid_request_body(),
        "max_tokens": 256,
        "temperature": 0.1,
        "tool_choice": "auto",
        "tools": [{"type": "function", "function": {"name": "x"}}],
    }
    response = _client(_RecordingBackend()).post("/v1/chat/completions", json=body)
    assert response.status_code == 200
    assert seen["max_tokens"] == 256
    assert seen["temperature"] == 0.1
    assert seen["tool_choice"] == "auto"


# ─── ERROR: input validation + model gating + stream gating ─────────────────


def test_unknown_model_returns_404() -> None:
    response = _client(NoopBackend()).post(
        "/v1/chat/completions",
        json=_valid_request_body(model="not-registered"),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


def test_stream_true_returns_sse_media_type_and_chunks() -> None:
    """``stream=True`` returns ``text/event-stream`` and the backend's
    chunks pass through unchanged."""
    chunks = (b"data: chunk-1\n\n", b"data: [DONE]\n\n")
    backend = NoopBackend(canned_stream_chunks=chunks)
    body = {**_valid_request_body(), "stream": True}

    response = _client(backend).post("/v1/chat/completions", json=body)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.content == b"".join(chunks)


def test_stream_unknown_model_returns_404_not_sse() -> None:
    """Pre-stream validation still gates — caller should see JSON 404,
    not an empty SSE stream."""
    body = {**_valid_request_body(model="not-registered"), "stream": True}
    response = _client(NoopBackend()).post("/v1/chat/completions", json=body)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


def test_stream_upstream_unavailable_returns_502_json() -> None:
    """Synchronous error contract — backend raises before first chunk,
    endpoint returns JSON 502 instead of starting an SSE stream."""

    class _DeadStreamBackend:
        async def ping(self) -> bool:
            return False

        async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("unused in stream test")

        async def stream(self, request: dict[str, Any]) -> Any:
            raise UpstreamUnavailableError("vLLM down")

    body = {**_valid_request_body(), "stream": True}
    response = _client(_DeadStreamBackend()).post("/v1/chat/completions", json=body)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_unavailable"


def test_stream_upstream_client_error_returns_sanitised_envelope() -> None:
    """The streaming path also sanitises — the response is JSON 422
    with our error envelope, not an SSE stream and not the raw body."""
    upstream_body = {"error": {"message": "no", "type": "invalid_request_error"}}

    class _BadInputStreamBackend:
        async def ping(self) -> bool:
            return True

        async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("unused in stream test")

        async def stream(self, request: dict[str, Any]) -> Any:
            raise UpstreamClientError(status_code=422, body=upstream_body)

    body = {**_valid_request_body(), "stream": True}
    response = _client(_BadInputStreamBackend()).post("/v1/chat/completions", json=body)
    assert response.status_code == 422
    payload = response.json()
    assert payload["error"]["code"] == "upstream_client_error"
    assert payload["error"]["status"] == 422
    assert payload["error"]["message"] == "no"
    assert "invalid_request_error" not in repr(payload)


def test_missing_messages_returns_422_via_pydantic() -> None:
    response = _client(NoopBackend()).post("/v1/chat/completions", json={"model": "selfhost-qwen"})
    assert response.status_code == 422


def test_empty_messages_list_returns_422() -> None:
    response = _client(NoopBackend()).post(
        "/v1/chat/completions",
        json={"model": "selfhost-qwen", "messages": []},
    )
    assert response.status_code == 422


# ─── ERROR: backend exceptions translated ───────────────────────────────────


def test_upstream_unavailable_maps_to_502() -> None:
    class _DeadBackend:
        async def ping(self) -> bool:
            return False

        async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
            raise UpstreamUnavailableError("vLLM down")

    response = _client(_DeadBackend()).post("/v1/chat/completions", json=_valid_request_body())
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_unavailable"


def test_tool_request_against_none_parser_model_returns_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a model registered with parser=NONE rejects tool
    requests with a structured 400 — never reaches the backend."""
    no_tool_model = ModelDefinition(
        served_name="no-tools",
        hf_repo="test/no-tools",
        tool_call_parser=ToolCallParser.NONE,
        max_model_len=4096,
        recommended_vram_gb=8,
    )
    extended = MappingProxyType({**DEFAULT_REGISTRY, "no-tools": no_tool_model})
    monkeypatch.setattr("llm_gateway.api.chat_completions.lookup_by_served_name", extended.get)

    body = {
        **_valid_request_body(model="no-tools"),
        "tools": [{"type": "function", "function": {"name": "x"}}],
    }
    response = _client(NoopBackend()).post("/v1/chat/completions", json=body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "model_does_not_support_tools"


def test_upstream_client_error_returns_sanitised_envelope() -> None:
    """Upstream 4xx bodies are NOT forwarded verbatim — only the
    human-readable message ends up in our envelope, wrapped under our
    own error code so internal vLLM fields cannot leak."""
    upstream_body = {
        "error": {
            "message": "you sent garbage",
            "type": "invalid_request_error",
            "internal_path": "/leak/should/not/escape",
        }
    }

    class _BadInputBackend:
        async def ping(self) -> bool:
            return True

        async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
            raise UpstreamClientError(status_code=400, body=upstream_body)

    response = _client(_BadInputBackend()).post("/v1/chat/completions", json=_valid_request_body())
    body = response.json()

    assert response.status_code == 400
    assert body == {
        "error": {
            "code": "upstream_client_error",
            "status": 400,
            "message": "you sent garbage",
        }
    }
    # No upstream-internal field leaks into our response.
    flat_response = repr(body)
    assert "internal_path" not in flat_response
    assert "invalid_request_error" not in flat_response


def test_upstream_client_error_message_is_length_capped() -> None:
    """A misbehaving upstream cannot inject arbitrarily large messages
    into our response."""
    upstream_body = {"error": {"message": "x" * 5000}}

    class _BadInputBackend:
        async def ping(self) -> bool:
            return True

        async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
            raise UpstreamClientError(status_code=400, body=upstream_body)

    response = _client(_BadInputBackend()).post("/v1/chat/completions", json=_valid_request_body())
    message = response.json()["error"]["message"]
    assert len(message) <= 1024
    assert message.startswith("xxxx")


def test_upstream_client_error_with_no_message_uses_generic_fallback() -> None:
    """vLLM occasionally emits a body without an error.message field;
    we fall back to a stable placeholder rather than crash."""

    class _OddBackend:
        async def ping(self) -> bool:
            return True

        async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
            raise UpstreamClientError(status_code=400, body={"odd": "shape"})

    response = _client(_OddBackend()).post("/v1/chat/completions", json=_valid_request_body())
    body = response.json()
    assert response.status_code == 400
    assert body["error"]["code"] == "upstream_client_error"
    assert body["error"]["message"] == "Upstream returned an error."
