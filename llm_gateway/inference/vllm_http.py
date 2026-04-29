"""HTTP proxy backend for the vendor vLLM OpenAI-compatible server.

The gateway and vLLM share a host; both bind to ``127.0.0.1``. This
backend speaks plain OpenAI-format HTTP to the vLLM container, keeping
the engine opaque. :meth:`aclose` releases the underlying
``httpx.AsyncClient`` and is wired into FastAPI's ``lifespan``
shutdown so SIGTERM drains in-flight requests before the pool tears
down.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from llm_gateway.inference.errors import UpstreamClientError, UpstreamUnavailableError


class VLLMHTTPBackend:
    """OpenAI-format HTTP client targeting a local vLLM container."""

    _CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
    _HEALTH_PATH = "/health"

    def __init__(
        self,
        *,
        upstream_url: str,
        request_timeout_s: float,
    ) -> None:
        # vLLM's OpenAI server already prefixes /v1; we mount the
        # AsyncClient at the bare base URL so paths read naturally.
        self._client = httpx.AsyncClient(
            base_url=upstream_url.rstrip("/"),
            timeout=request_timeout_s,
        )

    async def aclose(self) -> None:
        """Release the AsyncClient; called from FastAPI shutdown."""
        await self._client.aclose()

    async def ping(self) -> bool:
        """vLLM exposes ``/health``; treat any response other than 200 as down."""
        try:
            response = await self._client.get(self._HEALTH_PATH)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        """POST the chat-completion body to vLLM and return the parsed JSON."""
        try:
            response = await self._client.post(
                self._CHAT_COMPLETIONS_PATH,
                json=request,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise UpstreamUnavailableError(
                f"vLLM upstream not reachable: {type(exc).__name__}"
            ) from exc

        if response.status_code >= 500:
            raise UpstreamUnavailableError(f"vLLM upstream returned {response.status_code}")
        if response.status_code >= 400:
            # 4xx — forward as-is so the caller sees the real OpenAI error.
            try:
                body = response.json()
            except ValueError:
                body = {"error": {"message": response.text, "type": "upstream_4xx"}}
            raise UpstreamClientError(status_code=response.status_code, body=body)

        return response.json()

    async def stream(self, request: dict[str, Any]) -> AsyncIterator[bytes]:
        """Stream the upstream's SSE response chunks verbatim.

        Status-code checks happen synchronously (before the first chunk
        is yielded) so the API layer can return a non-stream JSON error
        when the upstream rejects or is unreachable. The async-iterator
        return value owns the response lifecycle — closing it (or fully
        consuming it) releases the connection back to the pool.
        """
        try:
            req = self._client.build_request("POST", self._CHAT_COMPLETIONS_PATH, json=request)
            response = await self._client.send(req, stream=True)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise UpstreamUnavailableError(
                f"vLLM upstream not reachable: {type(exc).__name__}"
            ) from exc

        if response.status_code >= 500:
            await response.aread()
            await response.aclose()
            raise UpstreamUnavailableError(f"vLLM upstream returned {response.status_code}")
        if response.status_code >= 400:
            body_bytes = await response.aread()
            await response.aclose()
            try:
                body = json.loads(body_bytes)
            except ValueError:
                body = {
                    "error": {
                        "message": body_bytes.decode("utf-8", errors="replace"),
                        "type": "upstream_4xx",
                    }
                }
            raise UpstreamClientError(status_code=response.status_code, body=body)

        async def _iter() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()

        return _iter()
