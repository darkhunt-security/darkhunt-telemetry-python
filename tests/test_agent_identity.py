"""``agent`` — per-agent topology node identity from a single client / Resource.

Port of ``test/trace-agent-identity.test.ts``. Two things have to hold together,
and the second is what makes the first safe:

1. ``service.name`` is emitted as a SPAN attribute on the root and on every child
   span, so the backend (which resolves a trace group's identity from merged
   attributes, where span attributes outrank the Resource) sees the agent rather
   than the process.
2. An agent-scoped trace is always a fresh ROOT — never parented under
   ``handoff_from[0]``, never under an ambient active span. Identity is resolved
   once per trace id, so two agents sharing a trace would collapse onto whichever
   name merged first. Upstreams stay ``agent_handoff`` links, which is what
   topology reconstruction resolves the edge from.
"""

from __future__ import annotations

from opentelemetry import context as context_api
from opentelemetry import trace as trace_api

from darkhunt_telemetry.attributes import ATTR
from darkhunt_telemetry.span import HANDOFF_LINK_KIND, LINK_KIND_ATTR
from darkhunt_telemetry.trace import Trace


def _trace(mem, **kwargs):
    kwargs.setdefault("tenant_id", "t1")
    kwargs.setdefault("workspace_id", "ws1")
    kwargs.setdefault("application_id", "app1")
    kwargs.setdefault("sanitizer", mem.sanitizer)
    return Trace(mem.tracer, **kwargs)


def _one(mem, name):
    found = mem.by_name(name)
    assert found, f'expected an exported span named "{name}"'
    return found[0]


def _trace_id(t: Trace) -> int:
    sc = trace_api.get_current_span(t.context).get_span_context()
    assert sc.trace_id, "expected a resolvable root context"
    return sc.trace_id


def _span_id(t: Trace) -> int:
    sc = trace_api.get_current_span(t.context).get_span_context()
    assert sc.span_id, "expected a resolvable root context"
    return sc.span_id


# --- span-level service.name -------------------------------------------------


def test_stamps_service_name_on_root(mem):
    t = _trace(mem, name="research.run", agent="research")
    t.end()

    assert _one(mem, "research.run").attributes[ATTR.SERVICE_NAME] == "research"


def test_stamps_service_name_on_every_child_span(mem):
    t = _trace(mem, name="research.run", agent="research")
    t.span("load-filings").end()
    t.generation("summarise").end()
    t.end()

    # A root-only value would move the root to the agent's node and strand the
    # subtree on the Resource's node — an empty agent card beside the real work.
    for name in ("research.run", "load-filings", "summarise"):
        assert _one(mem, name).attributes[ATTR.SERVICE_NAME] == "research", (
            f"{name} must carry the agent identity"
        )


def test_no_service_name_attribute_when_agent_unset(mem):
    t = _trace(mem, name="plain.run")
    t.span("child").end()
    t.end()

    # Falls through to the Resource service.name configured on the client.
    for name in ("plain.run", "child"):
        assert ATTR.SERVICE_NAME not in (_one(mem, name).attributes or {})


# --- one agent per trace id --------------------------------------------------


def test_agent_trace_does_not_nest_under_handoff(mem):
    research = _trace(mem, name="research.run", agent="research")
    scoring = _trace(
        mem,
        name="score.deal",
        agent="deal-scoring",
        handoff_from=[research.handoff_token()],
    )

    assert _trace_id(scoring) != _trace_id(research), (
        "an agent-scoped trace must not share a trace id with its upstream"
    )


def test_upstream_remains_an_agent_handoff_link(mem):
    research = _trace(mem, name="research.run", agent="research")
    research_root_id = _span_id(research)
    scoring = _trace(
        mem,
        name="score.deal",
        agent="deal-scoring",
        handoff_from=[research.handoff_token()],
    )
    scoring.end()
    research.end()

    root = _one(mem, "score.deal")
    assert len(root.links) == 1, "expected exactly one handoff link"
    assert root.links[0].context.span_id == research_root_id
    assert root.links[0].attributes[LINK_KIND_ATTR] == HANDOFF_LINK_KIND


def test_ambient_span_is_ignored_so_co_hosted_agents_never_merge(mem):
    # Stands in for a shared HTTP/server span both agents run beneath — the exact
    # case that would otherwise pull two agents into one trace id.
    server_span = mem.tracer.start_span("POST /deals")
    server_ctx = trace_api.set_span_in_context(server_span, context_api.get_current())
    token = context_api.attach(server_ctx)
    try:
        research = _trace(mem, name="research.run", agent="research")
        scoring = _trace(mem, name="score.deal", agent="deal-scoring")
    finally:
        context_api.detach(token)

    server_trace_id = server_span.get_span_context().trace_id
    assert _trace_id(research) != server_trace_id
    assert _trace_id(scoring) != server_trace_id
    assert _trace_id(research) != _trace_id(scoring)


def test_still_nests_under_handoff_when_agent_unset(mem):
    upstream = _trace(mem, name="caller")
    downstream = _trace(mem, name="callee", handoff_from=[upstream.handoff_token()])

    # Existing behaviour must be untouched for every caller that does not opt in.
    assert _trace_id(downstream) == _trace_id(upstream)
