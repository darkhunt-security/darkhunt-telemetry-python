"""Coverage for the mutation paths: Trace.update() and Span.update() field-by-field.

These re-emit attributes on the (still-open) root/child span; the existing suite
exercises the constructors but not the update() setters.
"""

from __future__ import annotations

import json

import pytest

from darkhunt_telemetry.attributes import ATTR, GEN_AI
from darkhunt_telemetry.trace import Trace


def _trace(mem, **kwargs):
    kwargs.setdefault("tenant_id", "t1")
    kwargs.setdefault("workspace_id", "ws1")
    kwargs.setdefault("application_id", "app1")
    kwargs.setdefault("sanitizer", mem.sanitizer)
    return Trace(mem.tracer, **kwargs)


def test_trace_update_all_routing_and_meta_fields(mem):
    t = _trace(mem, name="orig")
    t.update(
        name="renamed",
        tenant_id="t2",
        workspace_id="ws2",
        application_id="app2",
        assessment_run_id="run2",
        session_id="s9",
        user_id="u9",
        user_email="u9@x.com",
        tags=["a", "b"],
        metadata={"k": "v"},
        release="1.2.3",
        environment="staging",
        observation_type="chain",
        output={"answer": 42},
    )
    t.end()
    # Trace.update(name=...) updates the darkhunt.trace.name ATTRIBUTE but does
    # not rename the underlying OTel span (which keeps its start-time name),
    # unlike Span.update(name=...). So look the span up by its original name.
    (span,) = mem.by_name("orig")
    a = span.attributes
    assert a[ATTR.TENANT_ID] == "t2"
    assert a[ATTR.WORKSPACE_ID] == "ws2"
    assert a[ATTR.APPLICATION_ID] == "app2"
    assert a[ATTR.ASSESSMENT_RUN_ID] == "run2"
    assert a[ATTR.SESSION_ID] == "s9"
    assert a[ATTR.USER_ID] == "u9"
    assert a[ATTR.USER_EMAIL] == "u9@x.com"
    assert a[ATTR.TRACE_TAGS] == "a,b"
    assert a[ATTR.RELEASE] == "1.2.3"
    assert a[ATTR.ENVIRONMENT] == "staging"
    assert a[ATTR.OBSERVATION_TYPE] == "chain"
    assert a[ATTR.TRACE_NAME] == "renamed"
    assert a[f"{ATTR.METADATA_PREFIX}k"] == "v"
    assert json.loads(a[ATTR.OBSERVATION_OUTPUT])["answer"] == 42


def test_trace_update_returns_self_for_chaining(mem):
    t = _trace(mem)
    assert t.update(session_id="s1") is t
    t.end()


def test_span_update_fields(mem):
    t = _trace(mem)
    s = t.span("work")
    s.update(
        name="work2",
        input={"q": "hi"},
        output={"a": "bye"},
        metadata={"m": "1"},
        level="WARNING",
        status_message="careful",
        version="v3",
        tool_name="search",
        tool_call_id="call-1",
        tool_arguments={"query": "x"},
    )
    s.end()
    (span,) = mem.by_name("work2")
    a = span.attributes
    assert json.loads(a[ATTR.OBSERVATION_INPUT])["q"] == "hi"
    assert json.loads(a[ATTR.OBSERVATION_OUTPUT])["a"] == "bye"
    assert a[f"{ATTR.METADATA_PREFIX}m"] == "1"
    assert a[ATTR.OBSERVATION_LEVEL] == "WARNING"
    assert a[ATTR.STATUS_MESSAGE] == "careful"
    assert a[ATTR.VERSION] == "v3"
    assert a[GEN_AI.TOOL_NAME] == "search"
    assert a[GEN_AI.TOOL_CALL_ID] == "call-1"
    assert json.loads(a[GEN_AI.TOOL_CALL_ARGUMENTS])["query"] == "x"


def test_span_update_after_end_is_ignored_with_warning(mem):
    t = _trace(mem)
    s = t.span("s")
    s.end()
    with pytest.warns(UserWarning, match="already-ended"):
        s.update(output={"late": True})


def test_generation_update_model_usage_cost(mem):
    t = _trace(mem)
    g = t.generation("gen")
    g.update(
        model="claude-opus-4-8",
        model_parameters={"temperature": 0.2},
        usage={"input_tokens": 3, "output_tokens": 7},
        cost={"total": 0.01},
        completion_start_time=1_700_000_000.0,
        prompt_name="p",
        prompt_version="2",
        system_instructions="be brief",
    )
    g.end()
    (span,) = mem.by_name("gen")
    a = span.attributes
    assert a[ATTR.MODEL_NAME] == "claude-opus-4-8"
    assert a[GEN_AI.USAGE_INPUT_TOKENS] == 3
    assert a[GEN_AI.USAGE_COST] == pytest.approx(0.01)
    assert a[ATTR.PROMPT_NAME] == "p"
    assert a[ATTR.PROMPT_VERSION] == "2"
    assert a[GEN_AI.SYSTEM_INSTRUCTIONS] == "be brief"
    assert json.loads(a[ATTR.MODEL_PARAMETERS])["temperature"] == pytest.approx(0.2)
