"""Test-only entry point that boots ``llm-gateway`` with a ``NoopBackend``.

Used by cross-package integration tests that spawn a real gateway
subprocess and exercise it with an OpenAI-compatible client. Keeping
the launcher inside the test tree (not the production package) means
``__main__.py`` stays free of test-only branches. The only job here
is to translate environment-variable inputs into a :class:`NoopBackend`
configuration and run uvicorn — every cross-cutting concern still
lives in :func:`llm_gateway.app.create_app`.

Environment inputs (all optional except ``BEARER_TOKEN``):

* ``BEARER_TOKEN``                     — auth header the test will send
* ``SERVER_PORT``                      — bind port (defaults to 8000)
* ``LLM_GATEWAY_CANNED_RESPONSE_JSON`` — JSON body for non-stream
                                          ``/v1/chat/completions`` calls
* ``LLM_GATEWAY_CANNED_STREAM_B64``    — comma-separated base64 SSE
                                          chunks for stream calls
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys

import uvicorn

from llm_gateway.app import create_app
from llm_gateway.config import Settings
from llm_gateway.inference.noop import NoopBackend


def _build_canned_response() -> dict | None:
    raw = os.environ.get("LLM_GATEWAY_CANNED_RESPONSE_JSON")
    if not raw:
        return None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"launcher: malformed LLM_GATEWAY_CANNED_RESPONSE_JSON: {exc}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(decoded, dict):
        print(
            "launcher: LLM_GATEWAY_CANNED_RESPONSE_JSON must decode to an object", file=sys.stderr
        )
        sys.exit(2)
    return decoded


def _build_canned_stream_chunks() -> tuple[bytes, ...] | None:
    raw = os.environ.get("LLM_GATEWAY_CANNED_STREAM_B64")
    if not raw:
        return None
    try:
        return tuple(base64.b64decode(piece) for piece in raw.split(",") if piece)
    except (ValueError, binascii.Error) as exc:
        print(f"launcher: malformed LLM_GATEWAY_CANNED_STREAM_B64: {exc}", file=sys.stderr)
        sys.exit(2)


def main() -> None:
    settings = Settings()
    backend = NoopBackend(
        reachable=True,
        canned_response=_build_canned_response(),
        canned_stream_chunks=_build_canned_stream_chunks(),
    )
    app = create_app(settings=settings, backend=backend)
    uvicorn.run(
        app,
        host=settings.server_host,
        port=settings.server_port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
