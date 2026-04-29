"""``InferenceBackend`` Protocol — the gateway's only seam to the engine.

Protocol surface:

* ``ping()``       — readiness probe used by ``GET /ready``
* ``complete(...)`` — non-streaming chat completion
* ``stream(...)``   — server-sent event streaming
* ``aclose()``      — release any held resources on shutdown

The API layer depends on this Protocol via FastAPI ``Depends`` and
never imports a concrete backend class — swapping engines is a single
``create_app`` argument.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class InferenceBackend(Protocol):
    """Common surface every inference backend must satisfy."""

    async def ping(self) -> bool:
        """Return ``True`` iff the engine is reachable and ready to serve.

        Used by ``GET /ready`` to drive K8s-style readiness gating: the
        gateway process can be alive while the engine is still loading
        weights, and we don't want to take traffic until both are up.
        """
        ...

    async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        """Forward an OpenAI-format chat-completion request to the engine.

        ``request`` is the OpenAI-shape body (``model``, ``messages``,
        ``temperature``, ``tools``, …) the gateway has already validated
        — backends pass it through verbatim. The return value is the
        upstream response body, also OpenAI-format. Backends raise
        :class:`UpstreamUnavailableError` for connectivity / 5xx
        problems and :class:`UpstreamClientError` for upstream 4xx so
        the API layer can map them to the right HTTP status without
        any backend-specific knowledge.

        Streaming completion is :meth:`stream` so non-streaming callers
        do not import streaming machinery (ISP).
        """
        ...

    async def aclose(self) -> None:
        """Release any resources the backend holds.

        Wired into FastAPI's ``lifespan`` so a SIGTERM drains in-flight
        requests before tearing down the underlying httpx pool. Backends
        without state implement this as a no-op.
        """
        ...

    async def stream(self, request: dict[str, Any]) -> AsyncIterator[bytes]:
        """Stream OpenAI-format SSE chunks for a chat-completion request.

        Returns an :class:`AsyncIterator` that yields raw SSE bytes
        verbatim — the gateway forwards exactly what vLLM emits so any
        OpenAI-spec-compliant client (langchain-openai, openai-python,
        …) parses chunks without translation.

        Backends raise :class:`UpstreamUnavailableError` /
        :class:`UpstreamClientError` synchronously **before** the first
        chunk is yielded so the API layer can return a non-stream JSON
        error response when the upstream is unreachable or rejects the
        request. Errors that arise mid-stream (engine crash after first
        chunk) propagate through iterator __anext__; callers see them
        as a truncated SSE stream and translate via their own client
        logic — there is no clean HTTP status to return at that point.
        """
        ...
