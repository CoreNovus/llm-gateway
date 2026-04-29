"""``NoopBackend`` — a no-network stand-in for tests and local dev.

Useful for:

* unit-testing endpoint wiring without booting vLLM
* running ``python -m llm_gateway`` on a laptop to smoke ``/health`` and
  ``/ready`` before any real engine is configured

:class:`VLLMHTTPBackend` replaces this in production. ``canned_response``
and ``canned_stream_chunks`` let tests drive deterministic shapes
through ``/v1/chat/completions`` without speaking to a real engine.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

_DEFAULT_STREAM_CHUNKS: tuple[bytes, ...] = (
    b'data: {"choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n',
    b'data: {"choices":[{"index":0,"delta":{"content":"noop"}}]}\n\n',
    b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
    b"data: [DONE]\n\n",
)


@dataclass(frozen=True)
class NoopBackend:
    """``InferenceBackend`` implementation that never opens a socket.

    ``reachable`` controls :meth:`ping`; ``canned_response`` controls
    :meth:`complete` (default returns a stable shape derived from the
    request's ``model`` field); ``canned_stream_chunks`` controls
    :meth:`stream` (default yields a minimal OpenAI SSE handshake).
    """

    reachable: bool = True
    canned_response: dict[str, Any] | None = None
    canned_stream_chunks: tuple[bytes, ...] | None = None

    async def aclose(self) -> None:
        """No-op — NoopBackend holds no resources."""

    async def ping(self) -> bool:
        return self.reachable

    async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.canned_response is not None:
            return self.canned_response
        return {
            "id": "noop-completion",
            "object": "chat.completion",
            "created": 0,
            "model": request.get("model", "noop"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "noop response"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }

    async def stream(self, request: dict[str, Any]) -> AsyncIterator[bytes]:
        chunks = self.canned_stream_chunks or _DEFAULT_STREAM_CHUNKS

        async def _iter() -> AsyncIterator[bytes]:
            for chunk in chunks:
                yield chunk

        return _iter()
