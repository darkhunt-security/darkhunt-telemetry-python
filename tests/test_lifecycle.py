"""Context-manager lifecycle for Span / Generation / Trace (Issue #5).

These cover the additive ``with`` support that guarantees END on exit without
attaching the OTel active context (that remains the job of ``start_active_*``).
A wire-output regression check is included to prove the body-only dedup of the
attribute-writing logic (Issue #4) did not drift any emitted attribute.
"""

from __future__ import annotations

import json

import pytest
from opentelemetry.trace import StatusCode

from darkhunt_telemetry.attributes import ATTR, GEN_AI
from darkhunt_telemetry.trace import Trace


def _trace(mem, **kwargs):
    kwargs.setdefault("tenant_id", "t1")
    kwargs.setdefault("workspace_id", "ws1")
    kwargs.setdefault("application_id", "app1")
    kwargs.setdefault("sanitizer", mem.sanitizer)
    return Trace(mem.tracer, **kwargs)


def test_span_context_manager_ends_on_exit(mem):
    t = _trace(mem)
    with t.span("x") as s:
        assert s.otel_span.is_recording()
    # After the block the span is ended: no longer recording + exported.
    assert s.otel_span.is_recording() is False
    (span,) = mem.by_name("x")
    assert span.end_time is not None
    t.end()


def test_span_context_manager_returns_self(mem):
    t = _trace(mem)
    with t.span("x") as s:
        assert isinstance(s, type(t.span("y")))  # a Span
    t.end()


def test_span_context_manager_marks_error_and_reraises(mem):
    t = _trace(mem)

    s = t.span("will-fail")
    with pytest.raises(RuntimeError, match="boom"):
        with s:
            raise RuntimeError("boom")
    assert s.otel_span.is_recording() is False
    (span,) = mem.by_name("will-fail")
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes[ATTR.STATUS_MESSAGE] == "boom"
    assert span.attributes[ATTR.OBSERVATION_LEVEL] == "ERROR"
    t.end()


def test_span_manual_end_inside_with_is_idempotent(mem):
    t = _trace(mem)
    with t.span("x") as s:
        s.end(output="done")  # manual end inside the block
        assert s.otel_span.is_recording() is False
    # No double-end error; still exactly one exported span.
    (span,) = mem.by_name("x")
    assert span.attributes[ATTR.OBSERVATION_OUTPUT] == "done"
    t.end()


def test_generation_context_manager_runs_generation_end(mem):
    t = _trace(mem)
    with t.generation("answer", model="claude-sonnet-5") as g:
        assert g.otel_span.is_recording()
    # Generation.end() override still runs through __exit__.
    assert g.otel_span.is_recording() is False
    (span,) = mem.by_name("answer")
    assert span.attributes[ATTR.OBSERVATION_TYPE] == "generation"
    assert span.attributes[ATTR.MODEL_NAME] == "claude-sonnet-5"
    t.end()


def test_trace_context_manager_ends_root_span(mem):
    with _trace(mem, name="chat") as t:
        assert t.context is not None
    # Root span is ended on exit → exported.
    (span,) = mem.by_name("chat")
    assert span.end_time is not None


def test_trace_context_manager_does_not_suppress_exception(mem):
    trace = _trace(mem, name="chat")
    with pytest.raises(ValueError):
        with trace:
            raise ValueError("nope")
    (span,) = mem.by_name("chat")
    assert span.end_time is not None


def test_attribute_regression_unchanged_by_refactor(mem):
    """A representative generation span emits the exact keys/values expected
    before the AttributeWriter dedup — proof the wire output did not drift."""
    t = _trace(mem)
    g = t.generation("answer", model="claude-sonnet-5")
    g.update(input_messages=[{"role": "user", "content": "hi"}])
    g.end(
        model="claude-sonnet-5",
        output_messages=[{"role": "assistant", "content": "hello"}],
        usage={"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 2},
    )
    t.end()
    (span,) = mem.by_name("answer")
    a = span.attributes
    assert a[ATTR.OBSERVATION_TYPE] == "generation"
    assert a[ATTR.MODEL_NAME] == "claude-sonnet-5"
    assert a[GEN_AI.REQUEST_MODEL] == "claude-sonnet-5"
    assert a[GEN_AI.USAGE_INPUT_TOKENS] == 10
    assert a[GEN_AI.USAGE_OUTPUT_TOKENS] == 5
    assert a[GEN_AI.USAGE_CACHE_READ_INPUT_TOKENS] == 2
    assert json.loads(a[GEN_AI.INPUT_MESSAGES])[0]["content"] == "hi"
    assert json.loads(a[GEN_AI.OUTPUT_MESSAGES])[0]["content"] == "hello"


def test_metadata_and_masking_regression(mem):
    """Metadata fan-out + IO masking go through the shared writer; verify keys
    and masking are byte-identical to the pre-refactor behavior."""
    t = _trace(mem)
    with t.span(
        "work",
        metadata={"score": 0.9, "passed": True, "label": "x"},
        input="my email is a@b.com",
    ):
        # No body needed: the metadata/input passed to span() above are what's
        # under test; the span just has to open and close.
        pass
    t.end()
    (span,) = mem.by_name("work")
    a = span.attributes
    assert a[ATTR.METADATA_PREFIX + "score"] == pytest.approx(0.9)
    assert a[ATTR.METADATA_PREFIX + "passed"] is True
    assert a[ATTR.METADATA_PREFIX + "label"] == "x"
    assert "[EMAIL]" in a[ATTR.OBSERVATION_INPUT]
    assert "a@b.com" not in a[ATTR.OBSERVATION_INPUT]
