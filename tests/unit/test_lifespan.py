"""Lifespan tests — graceful shutdown calls ``backend.aclose``.

Drives the FastAPI lifecycle via ``TestClient``'s context manager;
the ``__exit__`` triggers shutdown.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from llm_gateway.app import create_app
from llm_gateway.config import Settings


class _SpyBackend:
    """Records aclose invocations."""

    def __init__(self) -> None:
        self.aclose_calls = 0

    async def aclose(self) -> None:
        self.aclose_calls += 1

    async def ping(self) -> bool:
        return True

    async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    async def stream(self, request: dict[str, Any]) -> Any:
        async def _gen() -> Any:
            yield b"chunk"

        return _gen()


def test_app_shutdown_calls_backend_aclose_exactly_once() -> None:
    backend = _SpyBackend()
    app = create_app(settings=Settings(bearer_token=""), backend=backend)

    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        # No aclose yet — still inside the lifespan context.
        assert backend.aclose_calls == 0

    # Exiting the with-block triggers shutdown.
    assert backend.aclose_calls == 1
