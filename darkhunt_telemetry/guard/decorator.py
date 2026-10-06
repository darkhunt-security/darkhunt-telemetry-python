"""``@guard`` — ask Darkhunt before a tool runs, and before its output is used.

A guarded call makes up to two checks against the guardrail manager's
``/verify``:

* ``TOOL_CALL`` with the arguments, before the function runs. A block here means
  the function never runs.
* ``TOOL_RESULT`` with what it returned (``after=True``, the default), before the
  caller sees it. A block here means the function ran but its output is withheld.

Each check is recorded as a ``guardrail`` span under the tool's span. The tool
span is the active one when the caller already opened it for this tool (it is
reused, not nested), otherwise the guard opens one under the current trace.
Outside any trace the checks still run — routed by the guard configuration — but
nothing is recorded and there is no session to tie them together.

The decorator does not depend on an agent framework: it wraps the function, so it
works wherever the function is called from. Put it *under* the framework's own
tool decorator, so the framework still reads the original name, docstring and
type hints (``functools.wraps`` carries them over)::

    @function_tool
    @guard
    def send_to_insurer(claim_id: str, documents: list[str]) -> str: ...
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import uuid
import warnings
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Callable, Dict, Iterator, Optional, Tuple, Union

from .._current import current_observation
from ..serialization import safe_json_dumps
from .client import VerifyClient
from .config import GuardConfig, get_config
from .verdict import DarkhuntBlocked, Verdict, refusal

OnDeny = Union[str, Callable[[Verdict], Any]]

# Framework context objects (the OpenAI Agents SDK's RunContextWrapper, a bound
# method's self) are not the tool's arguments and are often not serializable.
DEFAULT_EXCLUDE = ("self", "cls", "ctx", "context", "run_context", "wrapper")

_client = VerifyClient()
_warned: set = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        warnings.warn(message, stacklevel=4)


def _json_safe(value: Any) -> Any:
    """``value`` as plain JSON types, via the SDK's lenient encoder."""
    return json.loads(safe_json_dumps(value))


def _capped(value: Any, limit: int) -> Any:
    encoded = safe_json_dumps(value)
    if len(encoded.encode("utf-8")) <= limit:
        return json.loads(encoded)
    # Too big to send whole: send the beginning as text and say so. A rule on
    # content sees the first `limit` bytes; one on the tool name is unaffected.
    head = encoded.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
    return {"truncated": True, "bytes": len(encoded.encode("utf-8")), "head": head}


def _trace_of(host: Any) -> Any:
    if host is None:
        return None
    return getattr(host, "trace", host)  # a Span has .trace; a Trace is its own


def _service_name(trace: Any) -> Optional[str]:
    resource = getattr(getattr(trace, "_tracer_obj", None), "resource", None)
    attrs = getattr(resource, "attributes", None) or {}
    name = attrs.get("service.name")
    return str(name) if name else None


def record_verdict(g: Any, span: Any, v: Verdict) -> None:
    """Write one check onto its ``guardrail`` span ``g`` and, when it blocked,
    mark the tool ``span``. Shared with the AGT integration."""
    if g is None:
        return
    meta: Dict[str, Any] = {
        "guard.stage": v.stage,
        "guard.decision": v.decision or "NONE",
        "guard.blocked": v.blocked,
        "guard.mode": v.mode,
        "guard.latency_ms": round(v.latency_ms, 1),
    }
    if v.matched_rules:
        meta["guard.rule_id"] = v.matched_rules[0].rule_id
        meta["guard.rule_name"] = v.matched_rules[0].rule_name
        meta["guard.matched_rules"] = [r.rule_name for r in v.matched_rules]
    if v.observed_rules:
        meta["guard.observed_rules"] = [r.rule_name for r in v.observed_rules]
    if v.failed:
        meta["guard.failed"] = True
    if v.error:
        meta["guard.error"] = v.error
    g.update(metadata=meta, output={k.split(".", 1)[1]: val for k, val in meta.items()})
    flagged = v.denied or v.failed or v.unanswered or bool(v.observed_rules)
    if v.blocked:
        status = f"Blocked: {v.reason}"
    elif v.denied:
        status = f"Would block ({v.mode}): {v.reason}"
    elif v.observed_rules:
        status = f"Observed: {v.observed_rules[0].rule_name}"
    elif v.unanswered:
        status = v.reason
    else:
        status = None
    g.end(level="WARNING" if flagged else None, status_message=status)
    if v.blocked and getattr(span, "observation_type", None) == "tool":
        span.update(
            level="WARNING",
            status_message=status,
            metadata={"guard.blocked_at": v.stage, "guard.executed": v.stage != "TOOL_CALL"},
        )


def blocks(cfg: GuardConfig, v: Verdict) -> bool:
    """Whether ``v`` stops the step under ``cfg``'s mode and fail mode."""
    if cfg.mode != "enforce":
        return False
    if v.unanswered:
        return cfg.fail == "closed"
    return v.denied


class _Guarded:
    def __init__(
        self,
        fn: Callable[..., Any],
        name: str,
        after: bool,
        on_deny: OnDeny,
        exclude: Tuple[str, ...],
        config: Union[GuardConfig, Callable[[], GuardConfig], None],
    ) -> None:
        self.fn = fn
        self.name = name
        self.after = after
        self.on_deny = on_deny
        self.exclude = exclude
        self._config = config
        self._signature = inspect.signature(fn)

    # --- inputs ---
    def config(self) -> GuardConfig:
        if self._config is None:
            return get_config()
        return self._config() if callable(self._config) else self._config

    def arguments(self, a: tuple, kw: dict) -> Dict[str, Any]:
        """The call's arguments by parameter name — ``/verify`` takes an object."""
        try:
            bound = self._signature.bind_partial(*a, **kw)
        except TypeError:
            return _json_safe({"args": list(a), **kw})
        out: Dict[str, Any] = {}
        for pname, value in bound.arguments.items():
            if pname in self.exclude:
                continue
            kind = self._signature.parameters[pname].kind
            if kind is inspect.Parameter.VAR_KEYWORD:
                out.update(value)
            else:
                out[pname] = value
        return _json_safe(out)

    # --- the tool span ---
    @contextmanager
    def tool_span(self, host: Any, args: Dict[str, Any]) -> Iterator[Tuple[Any, bool]]:
        """``(span, opened)``. Reuses the active span when it is already this
        tool's; opens one under the current trace or span otherwise."""
        if host is None:
            yield None, False
            return
        if (
            getattr(host, "observation_type", None) == "tool"
            and getattr(host, "tool_name", None) == self.name
        ):
            yield host, False
            return
        with host.start_active_span(
            self.name, observation_type="tool", tool_name=self.name, tool_arguments=args
        ) as span:
            yield span, True

    # --- one check ---
    def request(
        self, cfg: GuardConfig, host: Any, stage: str, call_id: str, args: dict, result: Any
    ) -> Tuple[Optional[str], dict]:
        trace = _trace_of(host)
        tenant = getattr(trace, "tenant_id", None) or cfg.tenant_id
        tool: Dict[str, Any] = {"name": self.name, "callId": call_id, "arguments": args}
        if stage == "TOOL_RESULT":
            tool["result"] = _capped(result, cfg.max_result_bytes)
        body = {
            "stage": stage,
            "workspaceId": getattr(trace, "workspace_id", None) or cfg.workspace_id,
            "applicationId": getattr(trace, "application_id", None) or cfg.application_id,
            "sessionId": getattr(trace, "session_id", None),
            "userId": getattr(trace, "user_id", None),
            "userEmail": getattr(trace, "user_email", None),
            "source": cfg.source or getattr(trace, "agent", None) or _service_name(trace),
            "tool": tool,
        }
        return tenant, {k: v for k, v in body.items() if v}

    def check(
        self,
        cfg: GuardConfig,
        host: Any,
        span: Any,
        stage: str,
        call_id: str,
        args: dict,
        result: Any,
    ) -> Verdict:
        tenant, body = self.request(cfg, host, stage, call_id, args, result)
        if host is None:
            _warn_once(
                "no-run",
                f"darkhunt guard: {self.name} was called outside an active trace, so its "
                "checks carry no session — rules over a session's history cannot apply. "
                "Wrap the agent run in `with trace.activate():`.",
            )
        g = (
            span.span(f"darkhunt.guard.{stage.lower()}", observation_type="guardrail")
            if span is not None
            else None
        )
        if not tenant:
            _warn_once(
                "no-tenant", "darkhunt guard: no tenant id (DARKHUNT_TENANT_ID); checks are skipped"
            )
            v = Verdict(tool=self.name, stage=stage, decision=None, error="no tenant configured")
        else:
            timeout = cfg.call_timeout_s if stage == "TOOL_CALL" else cfg.result_timeout_s
            v = _client.verify(cfg, tenant_id=tenant, body=body, timeout_s=timeout)
        v = replace(v, mode=cfg.mode, blocked=blocks(cfg, v), session_id=body.get("sessionId"))
        self.record(g, span, v)
        if cfg.on_verdict is not None:
            try:
                cfg.on_verdict(v)
            except Exception as err:  # noqa: BLE001 — a reporting hook must not break the tool
                _warn_once("hook", f"darkhunt guard: on_verdict raised {err!r}")
        return v

    record = staticmethod(record_verdict)

    # --- what the caller gets instead ---
    def refuse(self, v: Verdict, span: Any, opened: bool) -> Any:
        if self.on_deny == "raise":
            raise DarkhuntBlocked(v)
        value = self.on_deny(v) if callable(self.on_deny) else refusal(v)
        if opened:
            span.update(output=value)
        return value

    # --- the call ---
    def call(self, a: tuple, kw: dict) -> Any:
        cfg = self.config()
        if cfg.mode == "off":
            return self.fn(*a, **kw)
        args = self.arguments(a, kw)
        host = current_observation()
        with self.tool_span(host, args) as (span, opened):
            call_id = (
                span.otel_span.get_span_context().span_id.to_bytes(8, "big").hex()
                if span is not None
                else uuid.uuid4().hex
            )
            pre = self.check(cfg, host, span, "TOOL_CALL", call_id, args, None)
            if pre.blocked:
                return self.refuse(pre, span, opened)
            result = self.fn(*a, **kw)
            if self.after:
                post = self.check(cfg, host, span, "TOOL_RESULT", call_id, args, result)
                if post.blocked:
                    return self.refuse(post, span, opened)
            if opened:
                span.update(output=result)
            return result

    async def acall(self, a: tuple, kw: dict) -> Any:
        cfg = self.config()
        if cfg.mode == "off":
            return await self.fn(*a, **kw)
        args = self.arguments(a, kw)
        host = current_observation()
        with self.tool_span(host, args) as (span, opened):
            call_id = (
                span.otel_span.get_span_context().span_id.to_bytes(8, "big").hex()
                if span is not None
                else uuid.uuid4().hex
            )
            pre = await self._acheck(cfg, host, span, "TOOL_CALL", call_id, args, None)
            if pre.blocked:
                return self.refuse(pre, span, opened)
            result = await self.fn(*a, **kw)
            if self.after:
                post = await self._acheck(cfg, host, span, "TOOL_RESULT", call_id, args, result)
                if post.blocked:
                    return self.refuse(post, span, opened)
            if opened:
                span.update(output=result)
            return result

    async def _acheck(
        self,
        cfg: GuardConfig,
        host: Any,
        span: Any,
        stage: str,
        call_id: str,
        args: dict,
        result: Any,
    ) -> Verdict:
        # requests is synchronous; to_thread keeps the event loop free and copies
        # the context, so the check still sees the current run.
        return await asyncio.to_thread(self.check, cfg, host, span, stage, call_id, args, result)


def guard(
    fn: Optional[Callable[..., Any]] = None,
    *,
    name: Optional[str] = None,
    after: bool = True,
    on_deny: OnDeny = "return",
    exclude: Tuple[str, ...] = DEFAULT_EXCLUDE,
    config: Union[GuardConfig, Callable[[], GuardConfig], None] = None,
) -> Any:
    """Guard a tool function. Use bare (``@guard``) or with options.

    ``name``      the tool name rules match on; defaults to the function's name.
    ``after``     also check the result before returning it (default ``True``).
    ``on_deny``   ``"return"`` (default) returns a refusal string the model can read;
                  ``"raise"`` raises :class:`DarkhuntBlocked`; a callable receives the
                  :class:`Verdict` and its return value is returned instead.
    ``exclude``   parameters left out of the arguments sent for checking.
    ``config``    a :class:`GuardConfig`, or a callable returning one, instead of the
                  process-wide configuration.
    """

    def wrap(f: Callable[..., Any]) -> Callable[..., Any]:
        if not (inspect.isroutine(f) or isinstance(f, functools.partial)):
            raise TypeError(
                f"darkhunt guard: cannot guard {f!r} — it is not a function. If a framework "
                "decorator already turned it into a tool object, put @guard below it."
            )
        if inspect.isgeneratorfunction(f) or inspect.isasyncgenfunction(f):
            raise TypeError(
                f"darkhunt guard: {getattr(f, '__name__', f)} is a generator; streaming tools "
                "cannot be guarded yet — their output is not complete until it has been consumed."
            )
        tool_name = name or getattr(f, "__name__", None) or repr(f)
        guarded = _Guarded(f, tool_name, after, on_deny, tuple(exclude), config)
        if inspect.iscoroutinefunction(f):

            @functools.wraps(f)
            async def async_wrapper(*a: Any, **kw: Any) -> Any:
                return await guarded.acall(a, kw)

            async_wrapper.__darkhunt_guard__ = guarded  # type: ignore[attr-defined]
            return async_wrapper

        @functools.wraps(f)
        def wrapper(*a: Any, **kw: Any) -> Any:
            return guarded.call(a, kw)

        wrapper.__darkhunt_guard__ = guarded  # type: ignore[attr-defined]
        return wrapper

    return wrap(fn) if fn is not None else wrap
