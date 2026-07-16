"""Dependency-free helpers for carrying a Darkhunt handoff token across a
service boundary. HTTP uses the standard W3C ``traceparent`` header; queue
transports use out-of-band message metadata that keeps the token out of the
business payload. Both import with zero Temporal packages installed (see
``darkhunt_telemetry.temporal`` for the optional Temporal interceptors)."""

from __future__ import annotations

from .http import (
    TRACEPARENT_HEADER,
    handoff_from_http_headers,
    handoff_to_http_headers,
)
from .queue import (
    HANDOFF_MESSAGE_META_KEY,
    handoff_from_message_meta,
    handoff_to_message_meta,
    handoffs_from_messages,
)

__all__ = [
    "TRACEPARENT_HEADER",
    "handoff_to_http_headers",
    "handoff_from_http_headers",
    "HANDOFF_MESSAGE_META_KEY",
    "handoff_to_message_meta",
    "handoff_from_message_meta",
    "handoffs_from_messages",
]
