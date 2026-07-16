"""OTel context/propagation bootstrap — the Python counterpart of
``src/otel-globals.ts``.

**Why this is nearly a no-op in Python (and is a real registration in Node).**
The TS SDK must install an ``AsyncLocalStorageContextManager`` before
``context.with(...)`` will nest spans. Python's OpenTelemetry context is
``contextvars``-based and always active, and the default global propagator is
already a composite of W3C TraceContext + Baggage. So span nesting and
``traceparent`` inject/extract work out of the box with **nothing to register**.

This function is kept for API symmetry with the TS SDK and to give apps an
explicit hook: it verifies a W3C TraceContext propagator is installed globally
and installs the default composite if — unusually — none is present. Called
automatically by the :class:`DarkhuntTelemetry` constructor unless disabled via
``register_context_manager=False`` (or ``DARKHUNT_REGISTER_CONTEXT_MANAGER=false``).
"""

from __future__ import annotations

_registered = False


def register_otel_context_globals() -> bool:
    """Ensure a global W3C propagator is available for ``traceparent``
    inject/extract. Idempotent; safe to call multiple times.

    Returns True if this call installed the default global propagator, False if
    one was already present (the common case in Python).
    """
    global _registered
    if _registered:
        return False
    _registered = True

    from opentelemetry import propagate
    from opentelemetry.propagators.textmap import TextMapPropagator

    current = propagate.get_global_textmap()
    # A propagator is considered present if it declares the ``traceparent`` field.
    fields = set()
    try:
        fields = set(current.fields) if isinstance(current, TextMapPropagator) else set()
    except Exception:  # pragma: no cover - defensive
        fields = set()

    if "traceparent" in fields:
        return False

    # No W3C propagator present — install the OTel default composite.
    from opentelemetry.baggage.propagation import W3CBaggagePropagator
    from opentelemetry.propagators.composite import CompositePropagator
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator,
    )

    propagate.set_global_textmap(
        CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
    )
    return True


__all__ = ["register_otel_context_globals"]
