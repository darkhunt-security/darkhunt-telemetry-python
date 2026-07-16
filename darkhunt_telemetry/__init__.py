"""Darkhunt telemetry SDK for Python.

Send LLM traces, generations, and observations to the Darkhunt platform for
persistence and security data enrichment. Built on OpenTelemetry primitives,
with built-in client-side data masking.

Quick start::

    from darkhunt_telemetry import DarkhuntTelemetry

    dh = DarkhuntTelemetry(
        api_key="dh-...",
        tenant_id="t1", workspace_id="ws-1", application_id="app-1",
    )
    trace = dh.trace("chat", session_id="sess-1", user_id="u-1")
    gen = trace.generation("answer", model="claude-sonnet-5")
    gen.update(input_messages=[{"role": "user", "content": "hi"}])
    gen.end(output_messages=[{"role": "assistant", "content": "hello"}],
            usage={"input_tokens": 10, "output_tokens": 5})
    trace.end()
    dh.flush()
"""

from __future__ import annotations

from ._version import __version__
from .client import DarkhuntTelemetry, MaskingOptions
from .masking import CustomPattern, Sanitizer
from .otel_globals import register_otel_context_globals
from .span import (
    Generation,
    HandoffToken,
    Span,
    span_context_to_token,
    token_to_context,
)
from .trace import Trace
from .transports import (
    HANDOFF_MESSAGE_META_KEY,
    TRACEPARENT_HEADER,
    handoff_from_http_headers,
    handoff_from_message_meta,
    handoff_to_http_headers,
    handoff_to_message_meta,
    handoffs_from_messages,
)
from .types import ChatMessage, Cost, Metadata, ObservationLevel, ObservationType, Usage

__all__ = [
    "__version__",
    # client
    "DarkhuntTelemetry",
    "MaskingOptions",
    # tracing
    "Trace",
    "Span",
    "Generation",
    "HandoffToken",
    "span_context_to_token",
    "token_to_context",
    # masking
    "Sanitizer",
    "CustomPattern",
    # otel
    "register_otel_context_globals",
    # types
    "ObservationType",
    "ObservationLevel",
    "Usage",
    "Cost",
    "Metadata",
    "ChatMessage",
    # transports
    "TRACEPARENT_HEADER",
    "handoff_to_http_headers",
    "handoff_from_http_headers",
    "HANDOFF_MESSAGE_META_KEY",
    "handoff_to_message_meta",
    "handoff_from_message_meta",
    "handoffs_from_messages",
]
