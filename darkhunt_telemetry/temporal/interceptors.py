"""Temporal interceptors that carry the Darkhunt handoff token in a TEMPORAL
HEADER (context propagation, with a per-edge override) — so the business args
stay pure.

Port of the TS SDK's ``src/temporal/*`` interceptors, adapted to the
``temporalio`` (Python) interceptor API:

- **Activity inbound** — read the incoming handoff header into an ambient
  contextvar exposed by :func:`current_handoff`; the activity passes
  ``handoff_from=current_handoff()`` to ``client.trace(...)``.
- **Workflow inbound** — capture this run's incoming handoff header.
- **Workflow outbound** — on ``start_child_workflow`` relocate a per-edge
  override (``HANDOFF_META`` in the child's dict arg, via
  :func:`~darkhunt_telemetry.temporal.child_args`) into the header and strip it,
  else propagate this workflow's own incoming header; on ``start_activity``
  propagate the incoming header so the activity nests under the same token.

Register :class:`HandoffInterceptor` in the worker's ``interceptors=[...]`` list.
It supplies both the activity and workflow interceptors, so a single entry wires
up the whole handoff path.

**Never instrument workflow code** (deterministic sandbox — no SDK): telemetry
lives in activities + the gateway. An activity retry re-runs its LLM+tool loop
and re-emits spans.
"""

from __future__ import annotations

import contextvars
from typing import Any, List, Optional, Type

import temporalio.converter
import temporalio.workflow
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    ExecuteWorkflowInput,
    Interceptor,
    StartActivityInput,
    StartChildWorkflowInput,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)

from .handoff_header import HANDOFF_HEADER, HANDOFF_META

_current_handoff: "contextvars.ContextVar[Optional[List[str]]]" = contextvars.ContextVar(
    "darkhunt_current_handoff", default=None
)


def current_handoff() -> Optional[List[str]]:
    """The upstream handoff token(s) this activity should nest under, read from
    the Temporal Header the workflow propagated. Returns ``None`` outside an
    activity, or when no handoff was propagated. Pass it straight to
    ``client.trace(handoff_from=current_handoff())``."""
    return _current_handoff.get()


def _decode_header(headers: Any, payload_converter: Any) -> Optional[List[str]]:
    payload = headers.get(HANDOFF_HEADER) if headers else None
    if payload is None:
        return None
    try:
        value = payload_converter.from_payload(payload)
    except Exception:
        return None
    return list(value) if isinstance(value, list) else None


class _HandoffActivityInbound(ActivityInboundInterceptor):
    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        payload_converter = temporalio.converter.default().payload_converter
        handoff = _decode_header(input.headers, payload_converter)
        if handoff is None:
            return await self.next.execute_activity(input)
        token = _current_handoff.set(handoff)
        try:
            return await self.next.execute_activity(input)
        finally:
            _current_handoff.reset(token)


class _HandoffWorkflowOutbound(WorkflowOutboundInterceptor):
    def __init__(self, next: WorkflowOutboundInterceptor, inbound: "_HandoffWorkflowInbound") -> None:
        super().__init__(next)
        self._inbound = inbound

    def _with_header(self, headers: Any, tokens: List[str]) -> dict:
        payload = temporalio.workflow.payload_converter().to_payload(tokens)
        merged = dict(headers) if headers else {}
        merged[HANDOFF_HEADER] = payload
        return merged

    def start_activity(self, input: StartActivityInput):
        incoming = self._inbound.incoming
        if incoming:
            input.headers = self._with_header(input.headers, incoming)
        return self.next.start_activity(input)

    async def start_child_workflow(self, input: StartChildWorkflowInput):
        args = list(input.args or [])
        first = next(iter(args), None)
        override = first.get(HANDOFF_META) if isinstance(first, dict) else None
        if override:
            # Per-edge override: relocate to the header and strip it from args.
            clean = {k: v for k, v in first.items() if k != HANDOFF_META}
            input.args = [clean, *args[1:]]
            input.headers = self._with_header(input.headers, list(override))
        elif self._inbound.incoming:
            input.headers = self._with_header(input.headers, self._inbound.incoming)
        return await self.next.start_child_workflow(input)


class _HandoffWorkflowInbound(WorkflowInboundInterceptor):
    def __init__(self, next: WorkflowInboundInterceptor) -> None:
        super().__init__(next)
        self.incoming: Optional[List[str]] = None

    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        self.next.init(_HandoffWorkflowOutbound(outbound, self))

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        incoming = _decode_header(input.headers, temporalio.workflow.payload_converter())
        if incoming:
            self.incoming = incoming
        return await self.next.execute_workflow(input)


class HandoffInterceptor(Interceptor):
    """Worker interceptor wiring the Darkhunt handoff propagation on both the
    activity and workflow sides. Register a single instance::

        worker = Worker(client, task_queue="...", workflows=[...], activities=[...],
                        interceptors=[HandoffInterceptor()])
    """

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        return _HandoffActivityInbound(next)

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> Optional[Type[WorkflowInboundInterceptor]]:
        return _HandoffWorkflowInbound


__all__ = ["HandoffInterceptor", "current_handoff"]
