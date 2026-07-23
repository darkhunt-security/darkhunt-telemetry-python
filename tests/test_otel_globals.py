"""Coverage for the global-propagator bootstrap.

``register_otel_context_globals`` mutates process-global OTel state, so each test
snapshots and restores both the module's ``_registered`` flag and the global
textmap propagator.
"""

from __future__ import annotations

import pytest
from opentelemetry import propagate

import darkhunt_telemetry.otel_globals as og


@pytest.fixture(autouse=True)
def _restore_global_state():
    saved_prop = propagate.get_global_textmap()
    saved_flag = og._registered
    try:
        yield
    finally:
        propagate.set_global_textmap(saved_prop)
        og._registered = saved_flag


def test_installs_default_when_no_traceparent_propagator(monkeypatch):
    """With no W3C propagator present, the bootstrap installs the composite."""

    class _NoFieldPropagator:
        # A propagator that advertises no fields (no 'traceparent').
        fields = frozenset()

        def inject(self, carrier, context=None, setter=None):  # pragma: no cover
            pass

        def extract(self, carrier, context=None, getter=None):  # pragma: no cover
            return context

    monkeypatch.setattr(og, "_registered", False)
    propagate.set_global_textmap(_NoFieldPropagator())

    installed = og.register_otel_context_globals()
    assert installed is True
    # A real W3C propagator is now present.
    assert "traceparent" in set(propagate.get_global_textmap().fields)


def test_noop_when_traceparent_already_present(monkeypatch):
    """When a W3C propagator is already global, the bootstrap does not replace
    it and reports that it installed nothing."""
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator,
    )

    monkeypatch.setattr(og, "_registered", False)
    propagate.set_global_textmap(TraceContextTextMapPropagator())

    assert og.register_otel_context_globals() is False


def test_idempotent_after_first_call(monkeypatch):
    """A second call short-circuits on the module flag and returns False."""
    monkeypatch.setattr(og, "_registered", True)
    assert og.register_otel_context_globals() is False
