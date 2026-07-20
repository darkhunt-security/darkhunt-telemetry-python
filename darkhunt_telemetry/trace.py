"""Trace — port of ``src/trace.ts``.

A :class:`Trace` is a single user-facing interaction. It owns an always-exported
root span carrying the routing fields (tenant / workspace / application) and is
the factory for the generations / spans / events beneath it. Multi-agent handoff
edges are expressed via ``handoff_from`` (which both nests the root under its
caller and records an ``agent_handoff`` link) and :meth:`handoff_token`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Literal, Optional, Sequence, Type, Union

from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.context import Context

from .attributes import ATTR
from .masking import Sanitizer
from .span import (
    ActiveChildHost,
    AttributeWriter,
    HandoffToken,
    span_context_to_token,
    to_otel_links,
    token_to_context,
)
from .types import Metadata, ObservationType

if TYPE_CHECKING:
    from types import TracebackType


def _to_handoff_contexts(
    handoff_from: Optional[Sequence[Union[HandoffToken, Context]]],
) -> List[Context]:
    """Normalize ``handoff_from`` entries (tokens or contexts) into OTel contexts."""
    if not handoff_from:
        return []
    out: List[Context] = []
    for h in handoff_from:
        ctx = token_to_context(h) if isinstance(h, str) else h
        if ctx is not None:
            out.append(ctx)
    return out


class Trace(ActiveChildHost):
    def __init__(
        self,
        tracer,
        *,
        name: Optional[str] = None,
        tenant_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        application_id: Optional[str] = None,
        assessment_run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        user_email: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
        metadata: Optional[Metadata] = None,
        release: Optional[str] = None,
        environment: Optional[str] = None,
        links: Optional[Sequence[Context]] = None,
        handoff_from: Optional[Sequence[Union[HandoffToken, Context]]] = None,
        observation_type: ObservationType = "agent",
        input: Any = None,
        output: Any = None,
        start_time: Optional[float] = None,
        sanitizer: Optional[Sanitizer] = None,
    ) -> None:
        self._tracer_obj = tracer
        self._sanitizer = sanitizer
        self._name = name
        # Routing fields are validated upstream by DarkhuntTelemetry.trace().
        self._tenant_id = tenant_id or ""
        self._workspace_id = workspace_id or ""
        self._application_id = application_id or ""
        self._assessment_run_id = assessment_run_id or ""
        self._session_id = session_id
        self._user_id = user_id
        self._user_email = user_email
        self._tags = list(tags) if tags else None
        self._metadata = metadata
        self._release = release
        self._environment = environment
        self._observation_type = observation_type or "agent"
        self._input = input
        self._output = output

        from .span import _to_nanos  # local import to avoid re-export churn

        handoff_contexts = _to_handoff_contexts(handoff_from)
        root_links = to_otel_links(list(links or []) + handoff_contexts)
        # Auto-parent the root under handoff_from[0] (the first resolvable handoff
        # context) so a downstream agent's trace NESTS under its caller — the
        # cross-service parentSpanId chain is what the platform reconstructs the
        # topology from. That upstream also stays an agent_handoff LINK. A declared
        # handoff_from[0] wins over any ambient active span. When handoff_from is
        # empty/unresolvable, fall back to the active context.
        parent_context = handoff_contexts[0] if handoff_contexts else context_api.get_current()
        self._root_span = tracer.start_span(
            self.mask_name(name or "trace"),
            context=parent_context,
            links=root_links or None,
            start_time=_to_nanos(start_time),
        )
        self._root_context = trace_api.set_span_in_context(self._root_span, parent_context)
        self._writer = AttributeWriter(self._root_span, self._sanitizer)
        self._apply_trace_attrs()

    # --- ActiveChildHost wiring ---
    @property
    def _tracer(self):
        return self._tracer_obj

    @property
    def _trace_ref(self) -> "Trace":
        return self

    @property
    def _parent_context(self) -> Context:
        return self._root_context

    # --- names / masking ---
    def mask_name(self, name: str) -> str:
        """Sanitize a span/trace name. Names land on the wire verbatim, so
        user-controlled values can leak; identifying fields like ``user_id`` /
        ``model`` are intentionally not masked, names are."""
        return self._sanitizer.sanitize(name) if self._sanitizer is not None else name

    # --- accessors ---
    @property
    def name(self) -> Optional[str]:
        return self._name

    @property
    def context(self) -> Context:
        """OTel context of the trace's root span."""
        return self._root_context

    def handoff_token(self) -> HandoffToken:
        """A serializable :data:`HandoffToken` for this agent's entry span. Pass
        it to a downstream agent's ``handoff_from`` to record the handoff as an
        ``agent_handoff`` span link. The root span is always exported, so it
        resolves."""
        return span_context_to_token(self._root_span.get_span_context())

    @property
    def sanitizer(self) -> Optional[Sanitizer]:
        return self._sanitizer

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    @property
    def application_id(self) -> str:
        return self._application_id

    @property
    def assessment_run_id(self) -> str:
        return self._assessment_run_id

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def user_id(self) -> Optional[str]:
        return self._user_id

    @property
    def user_email(self) -> Optional[str]:
        return self._user_email

    # --- mutation ---
    def update(
        self,
        *,
        name: Optional[str] = None,
        tenant_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        application_id: Optional[str] = None,
        assessment_run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        user_email: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
        metadata: Optional[Metadata] = None,
        release: Optional[str] = None,
        environment: Optional[str] = None,
        observation_type: Optional[ObservationType] = None,
        output: Any = None,
    ) -> "Trace":
        if name is not None:
            self._name = name
        if tenant_id is not None:
            self._tenant_id = tenant_id
        if workspace_id is not None:
            self._workspace_id = workspace_id
        if application_id is not None:
            self._application_id = application_id
        if assessment_run_id is not None:
            self._assessment_run_id = assessment_run_id
        if session_id is not None:
            self._session_id = session_id
        if user_id is not None:
            self._user_id = user_id
        if user_email is not None:
            self._user_email = user_email
        if tags is not None:
            self._tags = list(tags)
        if metadata is not None:
            self._metadata = metadata
        if release is not None:
            self._release = release
        if environment is not None:
            self._environment = environment
        if observation_type is not None:
            self._observation_type = observation_type
        if output is not None:
            self._output = output
        self._apply_trace_attrs()
        return self

    def end(self, end_time: Optional[float] = None) -> None:
        from .span import _to_nanos

        self._root_span.end(end_time=_to_nanos(end_time))

    # --- context manager (lifecycle only) ---
    def __enter__(self) -> "Trace":
        """Enter a ``with`` block that guarantees the trace's root span is ended
        on exit.

        NOTE: this only guarantees END; it does NOT make the root span the
        ACTIVE OTel context. Use the ``start_active_*`` helpers on the trace when
        you also need child/ambient spans to nest under it.
        """
        return self

    def __exit__(
        self,
        exc_type: "Optional[Type[BaseException]]",
        exc: "Optional[BaseException]",
        tb: "Optional[TracebackType]",
    ) -> Literal[False]:
        """End the root span on ``with``-block exit. Never suppresses the
        exception."""
        self.end()
        return False

    # --- internals ---
    def _apply_trace_attrs(self) -> None:
        span = self._root_span
        span.set_attribute(ATTR.OBSERVATION_TYPE, self._observation_type)
        span.set_attribute(ATTR.TENANT_ID, self._tenant_id)
        span.set_attribute(ATTR.WORKSPACE_ID, self._workspace_id)
        span.set_attribute(ATTR.APPLICATION_ID, self._application_id)
        span.set_attribute(ATTR.ASSESSMENT_RUN_ID, self._assessment_run_id)
        if self._name:
            span.set_attribute(ATTR.TRACE_NAME, self.mask_name(self._name))
        if self._session_id:
            span.set_attribute(ATTR.SESSION_ID, self._session_id)
        if self._user_id:
            span.set_attribute(ATTR.USER_ID, self._user_id)
        if self._user_email:
            span.set_attribute(ATTR.USER_EMAIL, self._user_email)
        if self._tags:
            if self._sanitizer is not None:
                tags = [self._sanitizer.sanitize(t) for t in self._tags]
            else:
                tags = self._tags
            span.set_attribute(ATTR.TRACE_TAGS, ",".join(tags))
        if self._release:
            span.set_attribute(ATTR.RELEASE, self._release)
        if self._environment:
            span.set_attribute(ATTR.ENVIRONMENT, self._environment)
        if self._metadata:
            self._writer.apply_metadata(self._metadata)
        self._writer.set_io(ATTR.OBSERVATION_INPUT, self._input)
        self._writer.set_io(ATTR.OBSERVATION_OUTPUT, self._output)


__all__ = ["Trace", "HandoffToken"]
