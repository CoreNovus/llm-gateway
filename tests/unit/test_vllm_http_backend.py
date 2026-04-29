"""``VLLMHTTPBackend`` tests using ``respx`` to stub the upstream."""

from __future__ import annotations

import httpx
import pytest
import respx

from llm_gateway.inference.errors import (
    UpstreamClientError,
    UpstreamUnavailableError,
)
from llm_gateway.inference.vllm_http import VLLMHTTPBackend

_UPSTREAM = "http://upstream.test"


def _backend() -> VLLMHTTPBackend:
    return VLLMHTTPBackend(upstream_url=_UPSTREAM, request_timeout_s=5.0)


# ─── ping ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_ping_returns_true_when_health_returns_200() -> None:
    respx.get(f"{_UPSTREAM}/health").mock(return_value=httpx.Response(200))
    backend = _backend()
    try:
        assert await backend.ping() is True
    finally:
        await backend.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_ping_returns_false_when_health_returns_503() -> None:
    respx.get(f"{_UPSTREAM}/health").mock(return_value=httpx.Response(503))
    backend = _backend()
    try:
        assert await backend.ping() is False
    finally:
        await backend.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_ping_returns_false_on_connection_error() -> None:
    respx.get(f"{_UPSTREAM}/health").mock(side_effect=httpx.ConnectError("nope"))
    backend = _backend()
    try:
        assert await backend.ping() is False
    finally:
        await backend.aclose()


# ─── complete: happy path ──────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_complete_forwards_body_and_returns_parsed_json() -> None:
    request_body = {"model": "selfhost-qwen", "messages": [{"role": "user", "content": "hi"}]}
    response_body = {"id": "x", "choices": []}
    route = respx.post(f"{_UPSTREAM}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=response_body)
    )

    backend = _backend()
    try:
        result = await backend.complete(request_body)
    finally:
        await backend.aclose()

    assert result == response_body
    sent = route.calls.last.request
    import json as _json

    assert _json.loads(sent.content) == request_body


# ─── complete: error mapping ───────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_complete_translates_upstream_5xx_to_unavailable() -> None:
    respx.post(f"{_UPSTREAM}/v1/chat/completions").mock(
        return_value=httpx.Response(503, text="overloaded")
    )
    backend = _backend()
    try:
        with pytest.raises(UpstreamUnavailableError, match="503"):
            await backend.complete({"model": "x", "messages": [{"role": "user", "content": "hi"}]})
    finally:
        await backend.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_complete_translates_timeout_to_unavailable() -> None:
    respx.post(f"{_UPSTREAM}/v1/chat/completions").mock(side_effect=httpx.TimeoutException("slow"))
    backend = _backend()
    try:
        with pytest.raises(UpstreamUnavailableError):
            await backend.complete({"model": "x", "messages": [{"role": "user", "content": "hi"}]})
    finally:
        await backend.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_complete_forwards_4xx_with_body_via_client_error() -> None:
    upstream_body = {"error": {"message": "bad model", "type": "invalid_request_error"}}
    respx.post(f"{_UPSTREAM}/v1/chat/completions").mock(
        return_value=httpx.Response(400, json=upstream_body)
    )
    backend = _backend()
    try:
        with pytest.raises(UpstreamClientError) as exc_info:
            await backend.complete({"model": "x", "messages": [{"role": "user", "content": "hi"}]})
    finally:
        await backend.aclose()
    assert exc_info.value.status_code == 400
    assert exc_info.value.body == upstream_body


@pytest.mark.asyncio
@respx.mock
async def test_complete_handles_4xx_with_non_json_body() -> None:
    respx.post(f"{_UPSTREAM}/v1/chat/completions").mock(
        return_value=httpx.Response(429, text="too fast")
    )
    backend = _backend()
    try:
        with pytest.raises(UpstreamClientError) as exc_info:
            await backend.complete({"model": "x", "messages": [{"role": "user", "content": "hi"}]})
    finally:
        await backend.aclose()
    assert exc_info.value.status_code == 429
    # Non-JSON bodies are wrapped into a stable shape so the API layer can pass it back.
    assert "upstream_4xx" in exc_info.value.body["error"]["type"]


# ─── stream: happy path + error mapping ────────────────────────────────────


_STREAM_REQUEST = {"model": "x", "messages": [{"role": "user", "content": "hi"}]}


@pytest.mark.asyncio
@respx.mock
async def test_stream_forwards_upstream_chunks_verbatim() -> None:
    sse_body = (
        b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    respx.post(f"{_UPSTREAM}/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, content=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    backend = _backend()
    try:
        iterator = await backend.stream(_STREAM_REQUEST)
        chunks = [chunk async for chunk in iterator]
    finally:
        await backend.aclose()

    assert b"".join(chunks) == sse_body


@pytest.mark.asyncio
@respx.mock
async def test_stream_translates_upstream_5xx_to_unavailable_before_yield() -> None:
    respx.post(f"{_UPSTREAM}/v1/chat/completions").mock(
        return_value=httpx.Response(503, text="overloaded")
    )
    backend = _backend()
    try:
        with pytest.raises(UpstreamUnavailableError, match="503"):
            await backend.stream(_STREAM_REQUEST)
    finally:
        await backend.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_stream_translates_upstream_4xx_to_client_error_with_body() -> None:
    upstream_body = {"error": {"message": "context too long", "type": "invalid_request_error"}}
    respx.post(f"{_UPSTREAM}/v1/chat/completions").mock(
        return_value=httpx.Response(400, json=upstream_body)
    )
    backend = _backend()
    try:
        with pytest.raises(UpstreamClientError) as exc_info:
            await backend.stream(_STREAM_REQUEST)
    finally:
        await backend.aclose()
    assert exc_info.value.status_code == 400
    assert exc_info.value.body == upstream_body


@pytest.mark.asyncio
@respx.mock
async def test_stream_translates_timeout_to_unavailable() -> None:
    respx.post(f"{_UPSTREAM}/v1/chat/completions").mock(side_effect=httpx.TimeoutException("slow"))
    backend = _backend()
    try:
        with pytest.raises(UpstreamUnavailableError):
            await backend.stream(_STREAM_REQUEST)
    finally:
        await backend.aclose()
