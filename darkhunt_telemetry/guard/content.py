"""Ask Darkhunt about a request before the agent sees it, and an answer before
the user does.

:func:`check_input` sends the request to ``/verify`` at the ``INPUT`` stage and
:func:`check_output` sends the answer at ``OUTPUT``. They make the same decision
as :func:`~darkhunt_telemetry.guard.guard` does for a tool call — same mode, fail
mode, routing and ``on_verdict`` hook — but stopping the work is the caller's
job, since only the caller knows what "don't run the agent" or "don't show the
answer" means::

    verdict = check_input(request)
    if verdict.blocked:
        return refusal(verdict)          # the agent never sees the request
    answer = run_agent(request)
    verdict = check_output(answer)
    return refusal(verdict) if verdict.blocked else answer

Each check is recorded as a ``guardrail`` span under the current trace or span.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from .._current import current_observation
from .config import GuardConfig, get_config
from .decorator import _client, _service_name, _trace_of, _warn_once, blocks, record_verdict
from .verdict import Verdict, refusal

Content = Union[str, Sequence[Dict[str, str]]]
ConfigArg = Union[GuardConfig, Callable[[], GuardConfig], None]


def _config(config: ConfigArg) -> GuardConfig:
    if config is None:
        return get_config()
    return config() if callable(config) else config


def _capped(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    # A content rule sees the first `limit` bytes; say so rather than send nothing.
    return encoded[:limit].decode("utf-8", errors="ignore") + f"\n[truncated: {len(encoded)} bytes]"


def _messages(content: Content, role: str, limit: int) -> List[Dict[str, str]]:
    if isinstance(content, str):
        return [{"role": role, "content": _capped(content, limit)}]
    return [
        {"role": str(m.get("role") or role), "content": _capped(str(m.get("content") or ""), limit)}
        for m in content
    ]


def _check(
    stage: str, content: Content, role: str, config: ConfigArg, session_id: Optional[str]
) -> Verdict:
    cfg = _config(config)
    if cfg.mode == "off":
        return Verdict(tool="", stage=stage, decision=None, mode="off")
    host = current_observation()
    trace = _trace_of(host)
    tenant = getattr(trace, "tenant_id", None) or cfg.tenant_id
    body: Dict[str, Any] = {
        "stage": stage,
        "workspaceId": getattr(trace, "workspace_id", None) or cfg.workspace_id,
        "applicationId": getattr(trace, "application_id", None) or cfg.application_id,
        "sessionId": getattr(trace, "session_id", None) or session_id,
        "userId": getattr(trace, "user_id", None),
        "userEmail": getattr(trace, "user_email", None),
        "source": cfg.source or getattr(trace, "agent", None) or _service_name(trace),
    }
    body = {k: v for k, v in body.items() if v}
    # Kept even when empty: /verify then answers (or rejects) the request itself.
    body["messages"] = messages = _messages(content, role, cfg.max_result_bytes)
    g = (
        host.span(f"darkhunt.guard.{stage.lower()}", observation_type="guardrail", input=messages)
        if host is not None
        else None
    )
    if not tenant:
        _warn_once(
            "no-tenant", "darkhunt guard: no tenant id (DARKHUNT_TENANT_ID); checks are skipped"
        )
        v = Verdict(tool="", stage=stage, decision=None, error="no tenant configured")
    else:
        # Content is classified by a model, so it gets the result budget, not the
        # tight one a tool call gets.
        v = _client.verify(cfg, tenant_id=tenant, body=body, timeout_s=cfg.result_timeout_s)
    v = replace(v, mode=cfg.mode, blocked=blocks(cfg, v), session_id=body.get("sessionId"))
    record_verdict(g, None, v)
    if cfg.on_verdict is not None:
        try:
            cfg.on_verdict(v)
        except Exception as err:  # noqa: BLE001 — a reporting hook must not break the run
            _warn_once("hook", f"darkhunt guard: on_verdict raised {err!r}")
    return v


def check_input(
    content: Content, *, session_id: Optional[str] = None, config: ConfigArg = None
) -> Verdict:
    """Check a request (``INPUT``) before the agent sees it.

    ``content`` is the request text, or a list of ``{"role", "content"}``
    messages when context matters. Routing and the session come from the current
    trace; ``session_id`` is used when the trace has none, or there is no trace (a
    gateway checking a run it has already handed off). Act on ``verdict.blocked``.
    """
    return _check("INPUT", content, "user", config, session_id)


def check_output(
    content: Content, *, session_id: Optional[str] = None, config: ConfigArg = None
) -> Verdict:
    """Check an answer (``OUTPUT``) before the user sees it. Act on ``verdict.blocked``."""
    return _check("OUTPUT", content, "assistant", config, session_id)


async def acheck_input(
    content: Content, *, session_id: Optional[str] = None, config: ConfigArg = None
) -> Verdict:
    # requests is synchronous; to_thread keeps the loop free and copies the
    # context, so the check still sees the current run.
    return await asyncio.to_thread(_check, "INPUT", content, "user", config, session_id)


async def acheck_output(
    content: Content, *, session_id: Optional[str] = None, config: ConfigArg = None
) -> Verdict:
    return await asyncio.to_thread(_check, "OUTPUT", content, "assistant", config, session_id)


__all__ = ["check_input", "check_output", "acheck_input", "acheck_output", "refusal", "Content"]
