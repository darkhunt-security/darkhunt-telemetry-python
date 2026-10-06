"""@guard against a stub /verify server: what reaches the server, what the caller
gets back, and what is recorded on the trace."""

from __future__ import annotations

import asyncio
import inspect
import json
import warnings

import pytest
from opentelemetry import trace as trace_api

from darkhunt_telemetry import current_observation
from darkhunt_telemetry.attributes import ATTR
from darkhunt_telemetry.guard import (
    DarkhuntBlocked,
    GuardConfig,
    configure_guard,
    guard,
    reset_config,
)
from darkhunt_telemetry.trace import Trace

META = ATTR.METADATA_PREFIX


def _configure(verify, **kw):
    kw.setdefault("mode", "enforce")
    kw.setdefault("fail", "open")
    return configure_guard(GuardConfig(url=verify.url, api_key="dh-test", **kw))


def _trace(mem, **kw):
    kw.setdefault("tenant_id", "t1")
    kw.setdefault("workspace_id", "ws1")
    kw.setdefault("application_id", "app1")
    return Trace(mem.tracer, name="agent", **kw)


@guard
def send_referral(patient_id: str, to: str) -> str:
    """Send a referral letter."""
    send_referral.calls += 1
    return f"sent {patient_id} to {to}"


send_referral.calls = 0


@pytest.fixture(autouse=True)
def _reset_calls():
    send_referral.calls = 0


# --- the request ---


def test_allowed_call_is_checked_before_and_after_with_the_runs_identity(mem, verify):
    _configure(verify)
    t = _trace(
        mem, session_id="claim-77", user_id="u1", user_email="u1@clinic.example", agent="claims"
    )
    with t.activate():
        out = send_referral("p-1", to="gp@stmarys.health")
    t.end()

    assert out == "sent p-1 to gp@stmarys.health"
    assert verify.stages() == [("TOOL_CALL", "send_referral"), ("TOOL_RESULT", "send_referral")]
    call, result = (r["body"] for r in verify.requests)
    assert verify.requests[0]["path"] == "/api/t/t1/verify"
    assert verify.requests[0]["headers"]["Authorization"] == "Bearer dh-test"
    assert call["workspaceId"] == "ws1" and call["applicationId"] == "app1"
    assert call["sessionId"] == "claim-77" and call["source"] == "claims"
    assert call["userId"] == "u1" and call["userEmail"] == "u1@clinic.example"
    assert call["tool"]["arguments"] == {"patient_id": "p-1", "to": "gp@stmarys.health"}
    assert "result" not in call["tool"]
    assert result["tool"]["result"] == "sent p-1 to gp@stmarys.health"
    assert result["tool"]["callId"] == call["tool"]["callId"]


def test_a_large_result_is_sent_truncated(mem, verify):
    _configure(verify, max_result_bytes=32)

    @guard
    def get_chart() -> dict:
        return {"notes": "x" * 500}

    with _trace(mem).activate():
        get_chart()
    sent = verify.requests[1]["body"]["tool"]["result"]
    assert sent["truncated"] is True and sent["bytes"] > 500 and len(sent["head"]) <= 32


def test_context_parameters_are_not_sent(mem, verify):
    _configure(verify)

    @guard
    def lookup(ctx, patient_id: str) -> str:
        return patient_id

    with _trace(mem).activate():
        lookup(object(), "p-1")
    assert verify.requests[0]["body"]["tool"]["arguments"] == {"patient_id": "p-1"}


# --- the verdicts ---


def test_deny_before_the_call_means_the_function_never_runs(mem, verify):
    _configure(verify)
    verify.deny("TOOL_CALL", "send_referral")
    with _trace(mem).activate():
        out = send_referral("p-1", to="x@other.example")

    assert send_referral.calls == 0
    assert out.startswith("Blocked by Darkhunt: No sending outside the care team")
    assert verify.stages() == [("TOOL_CALL", "send_referral")]


def test_deny_after_the_call_withholds_the_output(mem, verify):
    _configure(verify)
    verify.deny("TOOL_RESULT", "send_referral", rule="Prompt injection in tool output")
    with _trace(mem).activate():
        out = send_referral("p-1", to="gp@stmarys.health")

    assert send_referral.calls == 1
    assert out.startswith("Withheld by Darkhunt: Prompt injection in tool output")


def test_shadow_mode_records_a_deny_but_runs_the_tool(mem, verify):
    _configure(verify, mode="shadow", fail=None)
    verify.deny("TOOL_CALL", "send_referral")
    seen = []
    configure_guard(GuardConfig(url=verify.url, mode="shadow", on_verdict=seen.append))
    with _trace(mem).activate():
        out = send_referral("p-1", to="x@other.example")

    assert out == "sent p-1 to x@other.example"
    assert seen[0].denied and not seen[0].blocked
    (check,) = [s for s in mem.by_name("darkhunt.guard.tool_call")]
    assert check.attributes[ATTR.STATUS_MESSAGE].startswith("Would block (shadow)")


def test_observed_rules_are_reported_without_blocking(mem, verify):
    _configure(verify)
    verify.rules[("TOOL_CALL", "send_referral")] = {
        "decision": "ALLOW",
        "observedRules": [{"ruleId": "r-2", "ruleName": "Referral sent", "action": "DENY"}],
    }
    with _trace(mem).activate():
        assert send_referral("p-1", to="gp@stmarys.health").startswith("sent")
    (check,) = mem.by_name("darkhunt.guard.tool_call")
    assert check.attributes[ATTR.OBSERVATION_LEVEL] == "WARNING"
    assert json.loads(check.attributes[META + "guard.observed_rules"]) == ["Referral sent"]


def test_unreachable_darkhunt_follows_the_fail_mode(mem):
    configure_guard(
        GuardConfig(url="http://127.0.0.1:9", mode="enforce", fail="open", call_timeout_s=0.5)
    )
    with _trace(mem).activate():
        assert send_referral("p-1", to="a@b.example").startswith("sent")

    configure_guard(
        GuardConfig(url="http://127.0.0.1:9", mode="enforce", fail="closed", call_timeout_s=0.5)
    )
    with _trace(mem).activate():
        out = send_referral("p-1", to="a@b.example")
    assert out.startswith("Blocked by Darkhunt: Darkhunt unavailable")
    reset_config()


def test_on_deny_raise_and_callable(mem, verify):
    _configure(verify)
    verify.deny("TOOL_CALL", "get_meds")

    @guard(name="get_meds", on_deny="raise")
    def meds_raise() -> list:
        return ["warfarin"]

    @guard(name="get_meds", on_deny=lambda v: [])
    def meds_fallback() -> list:
        return ["warfarin"]

    with _trace(mem).activate():
        with pytest.raises(DarkhuntBlocked) as err:
            meds_raise()
        assert err.value.verdict.reason == "No sending outside the care team"
        assert meds_fallback() == []


def test_off_mode_makes_no_calls(mem, verify):
    _configure(verify, mode="off", fail=None)
    with _trace(mem).activate():
        assert send_referral("p-1", to="a@b.example").startswith("sent")
    assert verify.requests == []


# --- the trace ---


def test_checks_are_recorded_under_a_tool_span(mem, verify):
    _configure(verify)
    verify.deny("TOOL_CALL", "send_referral")
    t = _trace(mem)
    with t.activate():
        send_referral("p-1", to="x@other.example")
    t.end()

    (tool,) = mem.by_name("send_referral")
    (check,) = mem.by_name("darkhunt.guard.tool_call")
    assert tool.attributes[ATTR.OBSERVATION_TYPE] == "tool"
    assert tool.parent.span_id == mem.by_name("agent")[0].context.span_id
    assert check.parent.span_id == tool.context.span_id
    assert check.attributes[ATTR.OBSERVATION_TYPE] == "guardrail"
    assert check.attributes[META + "guard.decision"] == "DENY"
    assert check.attributes[META + "guard.rule_name"] == "No sending outside the care team"
    assert tool.attributes[ATTR.OBSERVATION_LEVEL] == "WARNING"
    assert tool.attributes[META + "guard.blocked_at"] == "TOOL_CALL"
    assert tool.attributes[META + "guard.executed"] is False


def test_an_existing_span_for_the_same_tool_is_reused(mem, verify):
    _configure(verify)
    t = _trace(mem)
    with t.start_active_span("send_referral", observation_type="tool", tool_name="send_referral"):
        send_referral("p-1", to="gp@stmarys.health")
    t.end()
    assert len(mem.by_name("send_referral")) == 1
    (tool,) = mem.by_name("send_referral")
    assert {s.parent.span_id for s in mem.spans() if s.name.startswith("darkhunt.guard.")} == {
        tool.context.span_id
    }


def test_outside_a_trace_checks_still_run_on_configured_routing(verify):
    _configure(verify, tenant_id="t9", workspace_id="ws9", application_id="app9")
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        assert send_referral("p-1", to="gp@stmarys.health").startswith("sent")
    body = verify.requests[0]["body"]
    assert verify.requests[0]["path"] == "/api/t/t9/verify"
    assert body["applicationId"] == "app9" and "sessionId" not in body


# --- context ---


def test_activate_makes_the_trace_current_and_restores_on_exit(mem):
    t = _trace(mem)
    assert current_observation() is None
    with t.activate():
        assert current_observation() is t
        assert trace_api.get_current_span() is t._root_span
        with t.start_active_span("step") as s:
            assert current_observation() is s
        assert current_observation() is t
    assert current_observation() is None
    t.end()


def test_async_tools_are_checked_and_see_the_run(mem, verify):
    _configure(verify)
    verify.deny("TOOL_RESULT", "fetch_labs")

    @guard
    async def fetch_labs(patient_id: str) -> dict:
        await asyncio.sleep(0)
        return {"hba1c": 7.2}

    async def run():
        with _trace(mem, session_id="s-async").activate():
            return await fetch_labs("p-1")

    out = asyncio.run(run())
    assert out.startswith("Withheld by Darkhunt")
    assert {r["body"]["sessionId"] for r in verify.requests} == {"s-async"}


# --- declaration ---


def test_the_wrapped_function_keeps_its_signature_and_docstring():
    assert send_referral.__name__ == "send_referral"
    assert send_referral.__doc__ == "Send a referral letter."
    assert list(inspect.signature(send_referral).parameters) == ["patient_id", "to"]


def test_enforce_requires_a_fail_mode():
    with pytest.raises(ValueError, match="fail mode"):
        GuardConfig(mode="enforce")


def test_framework_objects_and_generators_are_refused():
    class FunctionTool:  # what a framework decorator returns
        pass

    with pytest.raises(TypeError, match="below it"):
        guard(FunctionTool())

    with pytest.raises(TypeError, match="generator"):

        @guard
        def stream():
            yield 1
