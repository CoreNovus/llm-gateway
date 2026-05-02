"""``POST /v1/chat/completions`` — OpenAI-compatible passthrough.

The request body is Pydantic-validated, the configured ``model`` is
checked against the registry (multi-model gating), and the request is
forwarded verbatim to the backend. ``stream=True`` returns a
``text/event-stream`` SSE response; everything else is a normal JSON
body. The response body is returned untouched so vLLM stays the
source of truth for response shape.

Errors are translated 1-to-1:

* ``UpstreamUnavailableError`` (timeout / 5xx)            → ``502``
* ``UpstreamClientError`` (upstream 4xx with body)        → sanitised
                                                            envelope at
                                                            upstream status
* model not in registry                                   → ``404``
* tool-call request rejected by registry validation       → ``400``
"""

# NOTE: do NOT add ``from __future__ import annotations`` — FastAPI's
# ``Annotated[..., Depends(closure)]`` pattern needs eager evaluation
# to discover the embedded ``Depends`` (closure variables don't survive
# deferred-string evaluation). Same constraint as ``api/health.py``.

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse, Response, StreamingResponse

from llm_gateway.api.schemas import ChatCompletionRequest
from llm_gateway.inference.base import InferenceBackend
from llm_gateway.inference.errors import (
    UpstreamClientError,
    UpstreamUnavailableError,
)
from llm_gateway.models.registry import lookup_by_served_name
from llm_gateway.observability.metrics import Metrics
from llm_gateway.tool_calling.validation import validate_tool_request

_SSE_MEDIA_TYPE = "text/event-stream"
_SSE_HEADERS = {"Cache-Control": "no-cache"}

# Cap on the upstream-error message we surface back to the caller.
# vLLM 4xx bodies sometimes contain stack hints / file paths / model
# internals — we forward only the human-readable ``message`` field and
# clip its length so a malicious or misbehaving upstream cannot inject
# arbitrarily large payloads into the gateway response.
_UPSTREAM_MESSAGE_MAX_LEN = 1024


def make_chat_completions_router(
    backend_dep: Callable[[], InferenceBackend],
    metrics: Metrics | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["chat"])

    @router.post("/chat/completions")
    async def chat_completions(
        request: ChatCompletionRequest,
        backend: Annotated[InferenceBackend, Depends(backend_dep)],
    ) -> Response:
        model = lookup_by_served_name(request.model)
        if model is None:
            return JSONResponse(
                {
                    "error": {
                        "code": "model_not_found",
                        "message": f"Unknown model {request.model!r}.",
                    }
                },
                status_code=status.HTTP_404_NOT_FOUND,
            )

        # ``exclude_none`` strips the optional fields we never set so
        # vLLM sees the same body the caller sent — keeps the proxy
        # transparent for any fields we do not gate explicitly.
        payload = request.model_dump(exclude_none=True)

        rejection = validate_tool_request(payload, model)
        if rejection is not None:
            return JSONResponse(
                {"error": {"code": rejection.code, "message": rejection.message}},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        if request.stream:
            try:
                iterator = await backend.stream(payload)
            except UpstreamClientError as exc:
                return _client_error_response(exc)
            except UpstreamUnavailableError as exc:
                return _unavailable_response(exc)
            return StreamingResponse(
                iterator,
                media_type=_SSE_MEDIA_TYPE,
                headers=_SSE_HEADERS,
            )

        try:
            response_body = await backend.complete(payload)
        except UpstreamClientError as exc:
            return _client_error_response(exc)
        except UpstreamUnavailableError as exc:
            return _unavailable_response(exc)

        if metrics is not None:
            _record_token_usage(metrics, request.model, response_body)

        return JSONResponse(response_body)

    return router


def _record_token_usage(metrics: Metrics, model: str, response: dict) -> None:
    """Increment prompt + completion token counters from a non-stream response.

    Streaming responses don't expose ``usage`` until the final chunk
    (and only if ``stream_options={"include_usage":true}`` is set);
    instrumenting that path is deliberately out of scope here so the
    proxy stays a verbatim passthrough.

    Defensive on the upstream payload: a buggy or hostile vLLM that
    returns ``{"usage": {"prompt_tokens": "abc"}}`` must NOT raise out
    of this telemetry hook and surface as a 500 to the client. We
    treat any non-numeric value as a missing observation (0).
    """
    usage = response.get("usage") or {}
    prompt = _safe_token_count(usage.get("prompt_tokens"))
    completion = _safe_token_count(usage.get("completion_tokens"))
    if prompt:
        metrics.tokens_prompt_total.labels(model=model).inc(prompt)
    if completion:
        metrics.tokens_completion_total.labels(model=model).inc(completion)


def _safe_token_count(value: object) -> int:
    """Coerce an upstream token-count field to a non-negative ``int``.

    Returns 0 for ``None`` / missing / non-numeric / negative values.
    """
    try:
        count = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, count)


def _client_error_response(exc: UpstreamClientError) -> JSONResponse:
    """Build a sanitised 4xx response from an upstream client error.

    We do NOT forward the upstream body verbatim. vLLM 4xx bodies can
    contain stack hints, model paths, or other internals; we surface
    only the human-readable ``error.message`` (length-capped) inside
    our own envelope so the response shape is stable regardless of
    what the upstream chose to include.
    """
    return JSONResponse(
        {
            "error": {
                "code": "upstream_client_error",
                "status": exc.status_code,
                "message": _extract_upstream_message(exc.body),
            }
        },
        status_code=exc.status_code,
    )


def _unavailable_response(exc: UpstreamUnavailableError) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "upstream_unavailable", "message": exc.reason}},
        status_code=status.HTTP_502_BAD_GATEWAY,
    )


def _extract_upstream_message(body: dict) -> str:
    """Return the upstream's human message, length-capped, or a fallback.

    Accepts both the OpenAI-style ``{"error": {"message": "..."}}``
    shape and the bare-string ``{"error": "some text"}`` form vLLM
    occasionally emits. Anything else collapses to a generic
    placeholder so the caller always gets a string.

    Previously this called ``body.get("error", {}).get("message")``
    which raised ``AttributeError`` on the bare-string shape (str has
    no ``.get``) — defeating the documented fallback.
    """
    if not isinstance(body, dict):
        return "Upstream returned an error."
    err = body.get("error")
    if isinstance(err, dict):
        raw = err.get("message")
    elif isinstance(err, str):
        raw = err
    else:
        raw = None
    if not isinstance(raw, str) or not raw:
        return "Upstream returned an error."
    return raw[:_UPSTREAM_MESSAGE_MAX_LEN]
