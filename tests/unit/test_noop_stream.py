"""``NoopBackend.stream`` tests.

Default-shape vs. canned-shape coverage so endpoint tests can rely on
either path being deterministic.
"""

from __future__ import annotations

import pytest

from llm_gateway.inference.noop import NoopBackend


@pytest.mark.asyncio
async def test_default_stream_yields_openai_handshake_chunks() -> None:
    iterator = await NoopBackend().stream({"model": "selfhost-qwen", "messages": []})
    chunks = [chunk async for chunk in iterator]

    # OpenAI SSE: role delta → content delta → finish_reason delta → [DONE].
    assert any(b'"role":"assistant"' in chunk for chunk in chunks)
    assert any(b'"content":"noop"' in chunk for chunk in chunks)
    assert any(b'"finish_reason":"stop"' in chunk for chunk in chunks)
    assert chunks[-1] == b"data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_canned_stream_chunks_are_yielded_verbatim() -> None:
    canned = (b"a\n\n", b"b\n\n")
    backend = NoopBackend(canned_stream_chunks=canned)

    iterator = await backend.stream({"model": "x", "messages": []})
    chunks = [chunk async for chunk in iterator]

    assert tuple(chunks) == canned
