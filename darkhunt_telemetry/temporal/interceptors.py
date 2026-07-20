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
import warnings
from typing import Any, List, Optional, Type

import temporalio.activity
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


class HandoffHeaderWarning(UserWarning):
    """Emitted when a Temporal handoff header cannot be decoded, so the trace
    silently detaches from its upstream. Almost always a payload-converter
    mismatch: the header was encoded with a worker-configured ``DataConverter``
    (encryption codec, Pydantic, compression) but decoded with a different one.
    Filter it with ``warnings.filterwarnings("ignore",
    category=HandoffHeaderWarning)`` once you have confirmed the cause."""


def current_handoff() -> Optional[List[str]]:
    """The upstream handoff token(s) this activity should nest under, read from
    the Temporal Header the workflow propagated. Returns ``None`` outside an
    activity, or when no handoff was propagated. Pass it straight to
    ``client.trace(handoff_from=current_handoff())``."""
    return _current_handoff.get()


def _decode_header(
    headers: Any, payload_converter: Any, header_key: str = HANDOFF_HEADER
) -> Optional[List[str]]:
    """Decode the handoff token list from ``headers[header_key]`` using
    ``payload_converter``. Returns ``None`` when the header is absent. On a
    decode failure (or an unexpected shape) it does NOT raise — it emits a
    :class:`HandoffHeaderWarning` and returns ``None`` so the activity/workflow
    keeps running, merely detached from its upstream trace."""
    payload = headers.get(header_key) if headers else None
    if payload is None:
        return None
    try:
        value = payload_converter.from_payload(payload)
    except Exception as exc:
        warnings.warn(
            f"darkhunt-telemetry: failed to decode Temporal handoff header "
            f"{header_key!r} ({type(exc).__name__}: {exc}); the trace will be "
            f"detached from its upstream. This usually means the header was "
            f"encoded with a different payload converter than the one decoding "
            f"it — configure HandoffInterceptor with the worker's DataConverter.",
            HandoffHeaderWarning,
            stacklevel=2,
        )
        return None
    if isinstance(value, list):
        return list(value)
    warnings.warn(
        f"darkhunt-telemetry: Temporal handoff header {header_key!r} decoded to "
        f"{type(value).__name__}, expected a list; the trace will be detached "
        f"from its upstream.",
        HandoffHeaderWarning,
        stacklevel=2,
    )
    return None


class _HandoffActivityInbound(ActivityInboundInterceptor):
    def __init__(
        self,
        next: ActivityInboundInterceptor,
        payload_converter: Any = None,
        header_key: str = HANDOFF_HEADER,
    ) -> None:
        super().__init__(next)
        self._payload_converter = payload_converter
        self._header_key = header_key

    def _resolve_converter(self) -> Any:
        # Prefer the worker's configured converter, reached from the active
        # activity context (mirrors ``temporalio.workflow.payload_converter()``
        # on the workflow side). Fall back to an explicit constructor override,
        # then to the global default only when nothing else is reachable.
        try:
            return temporalio.activity.payload_converter()
        except Exception:  # nosec B110 - not in an activity context; fall back below
            pass
        if self._payload_converter is not None:
            return self._payload_converter
        return temporalio.converter.default().payload_converter

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        handoff = _decode_header(input.headers, self._resolve_converter(), self._header_key)
        if handoff is None:
            return await self.next.execute_activity(input)
        token = _current_handoff.set(handoff)
        try:
            return await self.next.execute_activity(input)
        finally:
            _current_handoff.reset(token)


class _HandoffWorkflowOutbound(WorkflowOutboundInterceptor):
    def __init__(
        self, next: WorkflowOutboundInterceptor, inbound: "_HandoffWorkflowInbound"
    ) -> None:
        super().__init__(next)
        self._inbound = inbound

    def _with_header(self, headers: Any, tokens: List[str]) -> dict:
        payload = temporalio.workflow.payload_converter().to_payload(tokens)
        merged = dict(headers) if headers else {}
        merged[self._inbound.header_key] = payload
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
        if override and isinstance(first, dict):
            # Per-edge override: relocate to the header and strip it from args.
            clean = {k: v for k, v in first.items() if k != HANDOFF_META}
            input.args = [clean, *args[1:]]
            input.headers = self._with_header(input.headers, list(override))
        elif self._inbound.incoming:
            input.headers = self._with_header(input.headers, self._inbound.incoming)
        return await self.next.start_child_workflow(input)


class _HandoffWorkflowInbound(WorkflowInboundInterceptor):
    #: Temporal Header key this workflow-side interceptor reads/propagates.
    #: Overridden per subclass by :meth:`HandoffInterceptor.workflow_interceptor_class`
    #: when a custom key is configured.
    header_key: str = HANDOFF_HEADER

    def __init__(self, next: WorkflowInboundInterceptor) -> None:
        super().__init__(next)
        self.incoming: Optional[List[str]] = None

    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        self.next.init(_HandoffWorkflowOutbound(outbound, self))

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        incoming = _decode_header(
            input.headers, temporalio.workflow.payload_converter(), self.header_key
        )
        if incoming:
            self.incoming = incoming
        return await self.next.execute_workflow(input)


class HandoffInterceptor(Interceptor):
    """Worker interceptor wiring the Darkhunt handoff propagation on both the
    activity and workflow sides. Register a single instance::

        worker = Worker(client, task_queue="...", workflows=[...], activities=[...],
                        interceptors=[HandoffInterceptor()])

    :param payload_converter: Fallback converter used by the activity-inbound
        side to decode the handoff header. Normally unnecessary — the interceptor
        reads the worker's configured converter from the active activity context
        (``temporalio.activity.payload_converter()``), so a worker built with a
        custom :class:`~temporalio.converter.DataConverter` (encryption codec,
        Pydantic, compression) decodes correctly with no extra wiring. Provide it
        only for the rare case where that automatic lookup is unavailable; the
        global default is used when neither is reachable.
    :param header_key: Temporal Header key carrying the handoff token array.
        Defaults to :data:`~darkhunt_telemetry.temporal.HANDOFF_HEADER`; override
        both worker sides by passing the same value here.
    """

    def __init__(
        self,
        *,
        payload_converter: Any = None,
        header_key: str = HANDOFF_HEADER,
    ) -> None:
        self._payload_converter = payload_converter
        self._header_key = header_key

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _HandoffActivityInbound(next, self._payload_converter, self._header_key)

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> Optional[Type[WorkflowInboundInterceptor]]:
        if self._header_key == HANDOFF_HEADER:
            return _HandoffWorkflowInbound
        # Bake the custom key onto a subclass so the sandboxed workflow
        # interceptor (instantiated with only ``next``) still sees it.
        return type(
            "_HandoffWorkflowInbound",
            (_HandoffWorkflowInbound,),
            {"header_key": self._header_key},
        )


__all__ = ["HandoffInterceptor", "HandoffHeaderWarning", "current_handoff"]
