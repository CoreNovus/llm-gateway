"""Correlation-ID context variable.

Threaded via a ``ContextVar`` so any logger / handler / dependency in the
request scope can read the current correlation_id without it travelling
through every signature. The logging middleware sets it at the start of
each request; tests reset it between cases.

Header name (``X-Correlation-ID``) matches the de-facto convention used
by ALBs, nginx, and most observability tooling. A future change may
map this to a W3C ``traceparent`` so spans tie back to a caller's OTel
trace; the simpler header is sufficient today.

Inbound values are validated against :data:`_VALID_PATTERN` — anything
else (oversized header, control characters, log-injection attempts) is
silently replaced by a fresh uuid4. The pattern intentionally accepts
the shapes commonly emitted by ALBs (hex), uuids (with or without
dashes), and short alphanumeric tags.
"""

import re
import uuid
from contextvars import ContextVar

CORRELATION_HEADER = "X-Correlation-ID"

# Letters / digits / dot / underscore / dash, length 1..128. Tight enough
# to defeat log-injection (no CR/LF) and to cap memory used by a hostile
# client stuffing the header; loose enough for any sane operator-chosen
# format.
_VALID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

_correlation_id_var: ContextVar[str | None] = ContextVar(
    "llm_gateway.correlation_id",
    default=None,
)


def current_correlation_id() -> str | None:
    """Return the correlation_id for the current request, or ``None``."""
    return _correlation_id_var.get()


def set_correlation_id(correlation_id: str) -> None:
    """Set the correlation_id for the current request scope."""
    _correlation_id_var.set(correlation_id)


def new_correlation_id() -> str:
    """Generate a fresh correlation_id when the caller did not provide one."""
    return uuid.uuid4().hex


def normalise_inbound_correlation_id(raw: str | None) -> str:
    """Return a safe correlation_id derived from the inbound header.

    Returns the inbound value when it matches :data:`_VALID_PATTERN`;
    otherwise mints a fresh uuid4 and discards the suspect input. The
    middleware calls this on every request so log lines and response
    headers carry only normalised values.
    """
    if raw and _VALID_PATTERN.match(raw):
        return raw
    return new_correlation_id()
