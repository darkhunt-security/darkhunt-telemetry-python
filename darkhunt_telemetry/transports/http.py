"""Carry a Darkhunt handoff token across an HTTP boundary in the standard W3C
``traceparent`` request header.

A :data:`~darkhunt_telemetry.span.HandoffToken` already IS a ``traceparent``
string, so these helpers are thin — their job is to name the intent and
centralize the header convention. Dependency-free (pure stdlib).

Producing side: merge the token onto outbound request headers with
:func:`handoff_to_http_headers`. Consuming side: read it back with
:func:`handoff_from_http_headers` and pass it to
``client.trace(handoff_from=[token])``.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

#: The standard W3C Trace Context header the handoff token travels in.
TRACEPARENT_HEADER = "traceparent"


def handoff_to_http_headers(
    token: str, headers: Optional[Mapping[str, str]] = None
) -> Dict[str, str]:
    """Merge a handoff token onto outbound request headers as ``traceparent``,
    returning a new dict (the input is not mutated)."""
    out: Dict[str, str] = dict(headers) if headers else {}
    out[TRACEPARENT_HEADER] = token
    return out


def handoff_from_http_headers(headers: Any) -> Optional[str]:
    """Read a handoff token back out of inbound request headers — a
    case-insensitive lookup of ``traceparent``. Returns ``None`` when absent or
    empty. Accepts a plain dict (values may be str or a repeated-header list) or
    any object with a case-insensitive ``.get(name)`` (e.g. a WSGI/Starlette
    ``Headers``). A repeated header resolves to its first entry."""
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    # A Mapping also has .get; distinguish a header-object .get (case-insensitive)
    # from a plain dict by whether the key is present verbatim. Try both safely.
    if callable(getter) and not isinstance(headers, dict):
        value = getter(TRACEPARENT_HEADER)
        return _first(value) or None
    if isinstance(headers, Mapping):
        for key, value in headers.items():
            if isinstance(key, str) and key.lower() == TRACEPARENT_HEADER:
                return _first(value) or None
    return None


def _first(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


__all__ = [
    "TRACEPARENT_HEADER",
    "handoff_to_http_headers",
    "handoff_from_http_headers",
]
