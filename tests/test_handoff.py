"""Handoff tokens: round-trip, nesting under caller, fan-in links."""

from __future__ import annotations

from darkhunt_telemetry.span import (
    HANDOFF_LINK_KIND,
    LINK_KIND_ATTR,
    span_context_to_token,
    token_to_context,
)
from darkhunt_telemetry.trace import Trace


def _trace(mem, **kwargs):
    kwargs.setdefault("tenant_id", "t1")
    kwargs.setdefault("workspace_id", "ws1")
    kwargs.setdefault("application_id", "app1")
    kwargs.setdefault("sanitizer", mem.sanitizer)
    return Trace(mem.tracer, **kwargs)


def test_handoff_token_is_traceparent():
    from opentelemetry.trace import SpanContext, TraceFlags

    sc = SpanContext(
        trace_id=0x0123456789ABCDEF0123456789ABCDEF,
        span_id=0x0123456789ABCDEF,
        is_remote=False,
        trace_flags=TraceFlags(1),
    )
    token = span_context_to_token(sc)
    assert token.startswith("00-")
    ctx = token_to_context(token)
    assert ctx is not None


def test_downstream_nests_under_caller(mem):
    upstream = _trace(mem, name="research-agent")
    token = upstream.handoff_token()
    upstream.end()

    downstream = _trace(mem, name="analyst-agent", handoff_from=[token])
    downstream.end()

    (up,) = mem.by_name("research-agent")
    (down,) = mem.by_name("analyst-agent")
    # Nested: same trace id, and downstream's parent is the upstream root span.
    assert down.context.trace_id == up.context.trace_id
    assert down.parent is not None
    assert down.parent.span_id == up.context.span_id


def test_handoff_creates_agent_handoff_link(mem):
    upstream = _trace(mem, name="a")
    token = upstream.handoff_token()
    upstream.end()

    downstream = _trace(mem, name="b", handoff_from=[token])
    downstream.end()

    (down,) = mem.by_name("b")
    assert len(down.links) == 1
    assert down.links[0].attributes[LINK_KIND_ATTR] == HANDOFF_LINK_KIND


def test_fan_in_first_is_parent_rest_are_links(mem):
    a = _trace(mem, name="a")
    b = _trace(mem, name="b")
    ta, tb = a.handoff_token(), b.handoff_token()
    a.end()
    b.end()

    c = _trace(mem, name="c", handoff_from=[ta, tb])
    c.end()

    (a_span,) = mem.by_name("a")
    (c_span,) = mem.by_name("c")
    # handoff_from[0] is the parent edge.
    assert c_span.parent.span_id == a_span.context.span_id
    # Both upstreams remain links (fan-in).
    assert len(c_span.links) == 2
