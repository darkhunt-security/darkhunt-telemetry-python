"""Trace / Span / Generation attribute emission + client routing behavior."""

from __future__ import annotations

import json

import pytest

from darkhunt_telemetry import DarkhuntTelemetry, MaskingOptions
from darkhunt_telemetry.attributes import ATTR, GEN_AI
from darkhunt_telemetry.trace import Trace


def _trace(mem, **kwargs):
    kwargs.setdefault("tenant_id", "t1")
    kwargs.setdefault("workspace_id", "ws1")
    kwargs.setdefault("application_id", "app1")
    kwargs.setdefault("sanitizer", mem.sanitizer)
    return Trace(mem.tracer, **kwargs)


def test_trace_root_attrs(mem):
    t = _trace(mem, name="chat", session_id="s1", user_id="u1", user_email="u@x.com")
    t.end()
    (span,) = mem.by_name("chat")
    a = span.attributes
    assert a[ATTR.TENANT_ID] == "t1"
    assert a[ATTR.WORKSPACE_ID] == "ws1"
    assert a[ATTR.APPLICATION_ID] == "app1"
    assert a[ATTR.OBSERVATION_TYPE] == "agent"
    assert a[ATTR.SESSION_ID] == "s1"
    assert a[ATTR.USER_ID] == "u1"
    assert a[ATTR.TRACE_NAME] == "chat"


def test_generation_full_payload(mem):
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


def test_masking_applied_to_io(mem):
    t = _trace(mem)
    g = t.generation("g")
    g.update(input_messages=[{"role": "user", "content": "my email is a@b.com"}])
    g.end()
    t.end()
    (span,) = mem.by_name("g")
    assert "[EMAIL]" in span.attributes[GEN_AI.INPUT_MESSAGES]
    assert "a@b.com" not in span.attributes[GEN_AI.INPUT_MESSAGES]


def test_tool_span_attrs(mem):
    t = _trace(mem)
    s = t.span("geocode", observation_type="tool", tool_name="geocode", tool_call_id="c1")
    s.end()
    t.end()
    (span,) = mem.by_name("geocode")
    assert span.attributes[ATTR.OBSERVATION_TYPE] == "tool"
    assert span.attributes[GEN_AI.TOOL_NAME] == "geocode"
    assert span.attributes[GEN_AI.TOOL_CALL_ID] == "c1"


def test_metadata_one_attr_per_key(mem):
    t = _trace(mem)
    s = t.span("work", metadata={"score": 0.9, "passed": True, "label": "x"})
    s.end()
    t.end()
    (span,) = mem.by_name("work")
    assert span.attributes[ATTR.METADATA_PREFIX + "score"] == 0.9
    assert span.attributes[ATTR.METADATA_PREFIX + "passed"] is True
    assert span.attributes[ATTR.METADATA_PREFIX + "label"] == "x"


def test_error_level_sets_status(mem):
    from opentelemetry.trace import StatusCode

    t = _trace(mem)
    s = t.span("boom")
    s.end(level="ERROR", status_message="it failed")
    t.end()
    (span,) = mem.by_name("boom")
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes[ATTR.OBSERVATION_LEVEL] == "ERROR"


def test_nested_span_parenting(mem):
    t = _trace(mem)
    parent = t.span("parent")
    child = parent.span("child")
    child.end()
    parent.end()
    t.end()
    (child_span,) = mem.by_name("child")
    (parent_span,) = mem.by_name("parent")
    assert child_span.parent.span_id == parent_span.context.span_id


def test_start_active_span_auto_nests_and_times(mem):
    t = _trace(mem)
    with t.start_active_span("outer") as outer:
        # A plain OTel span opened via the tracer with no explicit parent should
        # nest under the active span.
        inner = mem.tracer.start_span("ambient-child")
        inner.end()
    t.end()
    (ambient,) = mem.by_name("ambient-child")
    (outer_span,) = mem.by_name("outer")
    assert ambient.parent.span_id == outer_span.context.span_id


def test_start_active_span_marks_error_on_exception(mem):
    from opentelemetry.trace import StatusCode

    t = _trace(mem)
    with pytest.raises(RuntimeError):
        with t.start_active_span("will-fail"):
            raise RuntimeError("nope")
    t.end()
    (span,) = mem.by_name("will-fail")
    assert span.status.status_code == StatusCode.ERROR


# --- client-level behavior (no network; enabled=False uses a no-op tracer) ---


def test_client_requires_routing_fields():
    dh = DarkhuntTelemetry(enabled=False)
    with pytest.raises(ValueError):
        dh.trace("x")  # no tenant/workspace/application anywhere


def test_client_merges_routing_defaults():
    dh = DarkhuntTelemetry(
        enabled=False, tenant_id="t", workspace_id="w", application_id="a"
    )
    tr = dh.trace("x", session_id="s")
    assert tr.tenant_id == "t"
    assert tr.workspace_id == "w"
    assert tr.application_id == "a"
    assert tr.session_id == "s"


def test_per_trace_routing_overrides_default():
    dh = DarkhuntTelemetry(
        enabled=False, tenant_id="t", workspace_id="w", application_id="a"
    )
    tr = dh.trace("x", tenant_id="other")
    assert tr.tenant_id == "other"


def test_public_endpoint_requires_api_key(monkeypatch):
    monkeypatch.delenv("DARKHUNT_API_KEY", raising=False)
    with pytest.raises(ValueError):
        DarkhuntTelemetry(tenant_id="t", workspace_id="w", application_id="a")


def test_internal_endpoint_needs_no_api_key(monkeypatch):
    monkeypatch.delenv("DARKHUNT_API_KEY", raising=False)
    dh = DarkhuntTelemetry(
        internal=True, tenant_id="t", workspace_id="w", application_id="a"
    )
    assert dh.enabled
    dh.shutdown()


def test_masking_disabled_leaves_content_raw(mem):
    t = _trace(mem, sanitizer=None)
    g = t.generation("g")
    g.end(output="email a@b.com")
    t.end()
    (span,) = mem.by_name("g")
    assert span.attributes[ATTR.OBSERVATION_OUTPUT] == "email a@b.com"
