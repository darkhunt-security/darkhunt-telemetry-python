"""Darkhunt as the policy behind Microsoft's Agent Governance Toolkit (AGT).

AGT's Agent Control Specification (ACS) runtime stops an agent at intervention
points — before and after each tool call, on the run's input and output — and
asks a policy for a verdict. :class:`DarkhuntPolicy` is that policy for a
manifest entry of ``type: custom, adapter: darkhunt``. It asks Darkhunt's
``/verify``, so an agent governed by AGT is held to the rules in the Darkhunt
dashboard, lands in the same enforcement log, and records the same
``guardrail`` spans as one guarded with :func:`darkhunt_telemetry.guard.guard`::

    policies:
      darkhunt: {type: custom, adapter: darkhunt}
    intervention_points:
      pre_tool_call:  {policy: {id: darkhunt}, policy_target: $.tool_call.args}
      post_tool_call: {policy: {id: darkhunt}, policy_target: $.tool_result}

    control = AgentControl.from_path("agt.yaml", policy_dispatcher=DarkhuntPolicy())

:func:`agt_tool` puts a tool function behind such a control. AGT's own adapters
guard a framework's model client or run, not the tools inside a run, so this is
how the tools of e.g. an OpenAI Agents SDK agent reach ``pre_tool_call`` and
``post_tool_call``.

AGT is optional: ``pip install "agent-control-specification==0.3.1b1"`` (the
version in AGT's latest official release, v4.1.0; Python 3.11+). Nothing else
in this SDK imports it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import functools
import inspect
import uuid
from dataclasses import replace
from typing import Any, Callable, Dict, Mapping, Optional, Union

from ._current import current_observation
from .guard.config import GuardConfig, get_config
from .guard.decorator import (
    DEFAULT_EXCLUDE,
    _capped,
    _client,
    _json_safe,
    _service_name,
    _trace_of,
    _warn_once,
    record_verdict,
)
from .guard.verdict import Verdict

# The run each in-flight tool call belongs to, by the call id handed to AGT. ACS
# evaluates policies in a thread pool (`run_in_executor`), which does not carry
# context variables over, so the policy cannot read the current run itself.
_RUNS: Dict[str, Any] = {}

# ACS intervention point -> /verify stage. The model-call and lifecycle points
# have no /verify stage; they are allowed through untouched.
STAGES = {"pre_tool_call": "TOOL_CALL", "post_tool_call": "TOOL_RESULT"}


def _blocks(cfg: GuardConfig, v: Verdict) -> bool:
    if cfg.mode != "enforce":
        return False
    if v.unanswered:
        return cfg.fail == "closed"
    return v.denied


def _acs_verdict(v: Verdict) -> Dict[str, Any]:
    """A /verify verdict as an ACS verdict. ``warn`` lets the call through and
    keeps the reason in AGT's own telemetry."""
    rule = v.matched_rules[0] if v.matched_rules else None
    if v.blocked:
        code = f"darkhunt:{rule.rule_id}" if rule else "darkhunt:unavailable"
        return {"decision": "deny", "reason": code, "message": v.reason}
    if v.denied:
        return {
            "decision": "warn",
            "reason": "darkhunt:not_enforced",
            "message": f"Would block ({v.mode}): {v.reason}",
        }
    if v.observed_rules:
        return {
            "decision": "warn",
            "reason": "darkhunt:observed",
            "message": v.observed_rules[0].rule_name,
        }
    if v.unanswered:
        return {"decision": "warn", "reason": "darkhunt:unavailable", "message": v.reason}
    return {"decision": "allow"}


class DarkhuntPolicy:
    """ACS ``PolicyDispatcher`` that decides with Darkhunt's ``/verify``.

    Uses the guard configuration (``DARKHUNT_GUARD_*`` / :func:`configure_guard`)
    unless ``config`` is given. It never raises: ACS turns a dispatcher error into
    a deny, so an unreachable Darkhunt would always fail closed. Unanswered
    checks follow the configured fail mode instead.
    """

    def __init__(self, config: Union[GuardConfig, Callable[[], GuardConfig], None] = None) -> None:
        self._config = config

    def config(self) -> GuardConfig:
        if self._config is None:
            return get_config()
        return self._config() if callable(self._config) else self._config

    def evaluate(self, invocation: Mapping[str, Any]) -> Mapping[str, Any]:
        cfg = self.config()
        policy_input = invocation.get("input") or {}
        stage = STAGES.get(str(policy_input.get("intervention_point")))
        if stage is None or cfg.mode == "off":
            return {"decision": "allow"}
        try:
            return _acs_verdict(self._check(cfg, stage, policy_input.get("snapshot") or {}))
        except Exception as err:  # noqa: BLE001 — see the class docstring
            _warn_once("agt-error", f"darkhunt agt: check failed: {err!r}")
            closed = cfg.mode == "enforce" and cfg.fail == "closed"
            decision = "deny" if closed else "warn"
            return {"decision": decision, "reason": "darkhunt:error", "message": repr(err)}

    def _check(self, cfg: GuardConfig, stage: str, snapshot: Mapping[str, Any]) -> Verdict:
        call = snapshot.get("tool_call") or {}
        envelope = snapshot.get("envelope") or {}
        host = current_observation() or _RUNS.get(str(call.get("id")))
        trace = _trace_of(host)
        name = str(call.get("name") or "tool")
        args = call.get("args")
        tool: Dict[str, Any] = {
            "name": name,
            "callId": str(call.get("id") or uuid.uuid4().hex),
            "arguments": _json_safe(args if isinstance(args, dict) else {"value": args}),
        }
        if stage == "TOOL_RESULT":
            tool["result"] = _capped(snapshot.get("tool_result"), cfg.max_result_bytes)
        session = (envelope.get("session") or {}).get("id") or getattr(trace, "session_id", None)
        body = {
            "stage": stage,
            "workspaceId": getattr(trace, "workspace_id", None) or cfg.workspace_id,
            "applicationId": getattr(trace, "application_id", None) or cfg.application_id,
            "sessionId": session,
            "userId": (envelope.get("user") or {}).get("id") or getattr(trace, "user_id", None),
            "userEmail": getattr(trace, "user_email", None),
            "source": cfg.source
            or (envelope.get("agent") or {}).get("id")
            or getattr(trace, "agent", None)
            or _service_name(trace),
            "tool": tool,
        }
        body = {k: v for k, v in body.items() if v}
        tenant = envelope.get("tenant") or getattr(trace, "tenant_id", None) or cfg.tenant_id
        if not tenant:
            v = Verdict(tool=name, stage=stage, decision=None, error="no tenant configured")
        else:
            timeout = cfg.call_timeout_s if stage == "TOOL_CALL" else cfg.result_timeout_s
            v = _client.verify(cfg, tenant_id=tenant, body=body, timeout_s=timeout)
        v = replace(v, mode=cfg.mode, blocked=_blocks(cfg, v), session_id=session)
        if host is not None:
            g = host.span(f"darkhunt.guard.{stage.lower()}", observation_type="guardrail")
            record_verdict(g, host, v)
        if cfg.on_verdict is not None:
            try:
                cfg.on_verdict(v)
            except Exception as err:  # noqa: BLE001 — a reporting hook must not break the tool
                _warn_once("hook", f"darkhunt guard: on_verdict raised {err!r}")
        return v


def _envelope(host: Any = None) -> Dict[str, Any]:
    """The ACS snapshot envelope for a Darkhunt run (default: the current one)."""
    trace = _trace_of(host if host is not None else current_observation())
    if trace is None:
        return {}
    env: Dict[str, Any] = {}
    if getattr(trace, "session_id", None):
        env["session"] = {"id": trace.session_id}
    if getattr(trace, "agent", None):
        env["agent"] = {"id": trace.agent}
    if getattr(trace, "user_id", None):
        env["user"] = {"id": trace.user_id}
    return env


def _resolve(control: Any) -> Any:
    return control() if callable(control) and not hasattr(control, "run_tool") else control


def refusal(blocked: Any, tool_name: str) -> str:
    """The text a model reads in place of a blocked call's result."""
    verdict = blocked.result.verdict
    message = verdict.message or verdict.reason or "policy"
    if blocked.intervention_point.value == "pre_tool_call":
        return f"Blocked by Darkhunt: {message}. The {tool_name} tool was not run."
    return (
        f"Withheld by Darkhunt: {message}. The {tool_name} tool ran, but its output was withheld."
    )


async def run_governed(
    control: Any,
    tool_name: str,
    arguments: Mapping[str, Any],
    execute: Callable[[], Any],
    *,
    on_deny: Union[str, Callable[[Any], Any]] = "return",
    host: Any = None,
) -> Any:
    """Run one tool call through an AGT control: ``pre_tool_call``, ``execute()``,
    ``post_tool_call``. For agent loops that dispatch tools by name.

    ``execute`` runs the call as made (sync or async). ``control`` is as for
    :func:`agt_tool`; ``None`` just runs it. ``host`` is the observation to record
    under, when the caller is not inside the run's context.
    """
    ctl = _resolve(control)
    if ctl is None:
        result = execute()
        return await result if inspect.isawaitable(result) else result

    async def run(_effective_args: Any) -> Any:
        # Darkhunt never transforms arguments; the call runs as made.
        result = execute()
        return await result if inspect.isawaitable(result) else result

    from agent_control_specification import AgentControlBlocked

    call_id = uuid.uuid4().hex
    _RUNS[call_id] = host if host is not None else current_observation()
    try:
        ran = await ctl.run_tool(
            tool_name,
            _json_safe(dict(arguments)),
            run,
            tool_call_id=call_id,
            snapshot={"envelope": _envelope(_RUNS[call_id])},
        )
        return ran.value
    except AgentControlBlocked as blocked:
        if on_deny == "raise":
            raise
        if callable(on_deny):
            return on_deny(blocked)
        return refusal(blocked, tool_name)
    finally:
        _RUNS.pop(call_id, None)


def run_governed_sync(
    control: Any,
    tool_name: str,
    arguments: Mapping[str, Any],
    execute: Callable[[], Any],
    *,
    on_deny: Union[str, Callable[[Any], Any]] = "return",
    host: Any = None,
) -> Any:
    """:func:`run_governed` for synchronous code. Inside a running event loop the
    call moves to a worker thread (driving that loop from itself would deadlock),
    carrying the caller's context so the tool still sees the current run."""
    if _resolve(control) is None:
        return execute()
    coro = run_governed(control, tool_name, arguments, execute, on_deny=on_deny, host=host)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    ctx = contextvars.copy_context()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(ctx.run, asyncio.run, coro).result()


def agt_tool(
    control: Any,
    *,
    name: Optional[str] = None,
    exclude: tuple = DEFAULT_EXCLUDE,
    on_deny: Union[str, Callable[[Any], Any]] = "return",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Run a tool function through an AGT ``AgentControl`` (pre/post tool call).

    ``control`` is an ``AgentControl``, or a zero-argument callable returning one
    or ``None`` — ``None`` runs the tool unguarded, so AGT can stay switched off.
    The arguments are sent by parameter name (framework context objects left
    out), and the current Darkhunt run supplies the session and agent. Put it
    *under* the framework's tool decorator; a sync function stays sync.
    ``on_deny`` is as for ``@guard``: ``"return"`` a refusal the model can read,
    ``"raise"``, or a callable that gets AGT's ``AgentControlBlocked``.
    """

    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        signature = inspect.signature(fn)
        tool_name = name or fn.__name__

        def arguments(a: tuple, kw: dict) -> Dict[str, Any]:
            bound = signature.bind_partial(*a, **kw)
            out: Dict[str, Any] = {}
            for pname, value in bound.arguments.items():
                if pname in exclude:
                    continue
                if signature.parameters[pname].kind is inspect.Parameter.VAR_KEYWORD:
                    out.update(value)
                else:
                    out[pname] = value
            return out

        if inspect.iscoroutinefunction(fn):

            async def acall(*a: Any, **kw: Any) -> Any:
                return await run_governed(
                    control, tool_name, arguments(a, kw), lambda: fn(*a, **kw), on_deny=on_deny
                )

            return functools.wraps(fn)(acall)

        def call(*a: Any, **kw: Any) -> Any:
            return run_governed_sync(
                control, tool_name, arguments(a, kw), lambda: fn(*a, **kw), on_deny=on_deny
            )

        return functools.wraps(fn)(call)

    return wrap


async def check_tool_point(
    control: Any,
    point: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    result: Any = None,
    host: Any = None,
) -> Optional[str]:
    """Ask one tool intervention point (``"pre_tool_call"`` / ``"post_tool_call"``)
    without running the tool, for frameworks that only offer allow/deny hooks
    (e.g. the Claude Agent SDK's PreToolUse / PostToolUse). Returns the refusal
    text when the policy denies, else ``None``."""
    ctl = _resolve(control)
    if ctl is None:
        return None
    from agent_control_specification import InterventionPoint

    call_id = uuid.uuid4().hex
    _RUNS[call_id] = host if host is not None else current_observation()
    snapshot: Dict[str, Any] = {
        "envelope": _envelope(_RUNS[call_id]),
        "tool_call": {"name": tool_name, "args": _json_safe(dict(arguments)), "id": call_id},
    }
    if point == "post_tool_call":
        snapshot["tool_result"] = _json_safe(result)
    try:
        outcome = await ctl.evaluate_intervention_point(
            InterventionPoint(point), snapshot, "enforce"
        )
    finally:
        _RUNS.pop(call_id, None)
    verdict = outcome.verdict
    if str(getattr(verdict.decision, "value", verdict.decision)) != "deny":
        return None
    message = verdict.message or verdict.reason or "policy"
    if point == "pre_tool_call":
        return f"Blocked by Darkhunt: {message}. The {tool_name} tool was not run."
    return f"Withheld by Darkhunt: {message}. Do not use the output of {tool_name}."
