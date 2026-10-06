"""Span + Generation — port of ``src/span.ts``.

A :class:`Span` wraps an OTel span and applies the Darkhunt attribute schema
(routing attrs, input/output, metadata, tool fields). A
:class:`Generation` is a Span specialized for LLM round-trips (model / usage /
cost). Both are created through a :class:`~darkhunt_telemetry.trace.Trace` (or a
parent Span) so routing context flows down automatically.
"""

from __future__ import annotations

import math
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Literal, Optional, Sequence, Type

from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.context import Context
from opentelemetry.trace import (
    Link,
    NonRecordingSpan,
    SpanContext,
    Status,
    StatusCode,
    TraceFlags,
)
from opentelemetry.trace import (
    Span as OtelSpan,
)
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from ._current import use_observation
from .attributes import ATTR, GEN_AI
from .serialization import safe_json_dumps
from .types import ChatMessage, Cost, Metadata, ObservationLevel, ObservationType, Usage

if TYPE_CHECKING:  # avoid a runtime import cycle with trace.py
    from types import TracebackType

    from .trace import Trace

# A HandoffToken is an opaque, serializable W3C ``traceparent`` string.
HandoffToken = str

_PROPAGATOR = TraceContextTextMapPropagator()

# Link attribute key + value marking a span link as an agent handoff, so a
# topology consumer can tell handoffs apart from any other use of OTel links.
LINK_KIND_ATTR = "darkhunt.link.kind"
HANDOFF_LINK_KIND = "agent_handoff"


def span_context_to_token(sc: Optional[SpanContext]) -> HandoffToken:
    """Serialize a span context into a W3C ``traceparent`` string via the global
    propagator, with a direct fallback."""
    if sc is None or not sc.is_valid:
        return ""
    carrier: Dict[str, str] = {}
    ctx = trace_api.set_span_in_context(NonRecordingSpan(sc))
    _PROPAGATOR.inject(carrier, context=ctx)
    tp = carrier.get("traceparent")
    if tp:
        return tp
    flags = format(int(sc.trace_flags) & 0xFF, "02x")
    return f"00-{format(sc.trace_id, '032x')}-{format(sc.span_id, '016x')}-{flags}"


def _to_nanos(seconds: Optional[float]) -> Optional[int]:
    """Convert an epoch-seconds timestamp (the Python convention, e.g.
    ``time.time()``) to the integer nanoseconds OTel spans want. ``None`` passes
    through."""
    if seconds is None:
        return None
    return int(seconds * 1_000_000_000)


class AttributeWriter:
    """The single place that knows how to write a value as an OTel attribute for
    the Darkhunt schema. Both :class:`Span` and
    :class:`~darkhunt_telemetry.trace.Trace` wrap their underlying OTel span in
    one of these so serialization behaves identically on the wire, regardless of
    who is emitting. Values are written verbatim.
    """

    __slots__ = ("_span",)

    def __init__(self, span: OtelSpan) -> None:
        self._span = span

    def set_io(self, key: str, value: Any) -> None:
        """Set an input/output-style attribute: store strings verbatim and
        everything else as a JSON string. ``None`` is skipped."""
        if value is None:
            return
        if isinstance(value, str):
            self._span.set_attribute(key, value)
        else:
            self._span.set_attribute(key, safe_json_dumps(value))

    def set_string(self, key: str, value: Optional[str]) -> None:
        """Set a string attribute; falsy values (``None``/empty) skipped."""
        if value:
            self._span.set_attribute(key, value)

    def set_json(self, key: str, value: Any) -> None:
        """Store a structured value as a JSON string. ``None`` is skipped."""
        if value is None:
            return
        self._span.set_attribute(key, safe_json_dumps(value))

    def apply_metadata(self, metadata: Metadata) -> None:
        """Fan ``metadata`` out into one ``METADATA_PREFIX + key`` attribute per
        entry."""
        for k, v in metadata.items():
            if v is None:
                continue
            key = f"{ATTR.METADATA_PREFIX}{k}"
            if isinstance(v, (str, int, float)):  # bool is an int subclass
                self._span.set_attribute(key, v)
            else:
                self._span.set_attribute(key, safe_json_dumps(v))


def apply_metadata_attrs(span: OtelSpan, metadata: Metadata) -> None:
    """Module-level shim delegating to :meth:`AttributeWriter.apply_metadata`."""
    AttributeWriter(span).apply_metadata(metadata)


def to_otel_links(contexts: "Optional[Sequence[Context]]") -> List[Link]:
    """Resolve caller-supplied contexts into OTel span links (valid ones only),
    each tagged as an agent handoff."""
    if not contexts:
        return []
    links: List[Link] = []
    for c in contexts:
        sc = trace_api.get_current_span(c).get_span_context()
        if sc.is_valid:
            links.append(Link(sc, attributes={LINK_KIND_ATTR: HANDOFF_LINK_KIND}))
    return links


@dataclass
class _SpanOptions:
    input: Any = None
    output: Any = None
    metadata: Optional[Metadata] = None
    level: Optional[ObservationLevel] = None
    status_message: Optional[str] = None
    version: Optional[str] = None
    observation_type: ObservationType = "span"
    links: Optional[Sequence[Context]] = None
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_arguments: Any = None
    start_time: Optional[float] = None


@dataclass
class _GenerationOptions(_SpanOptions):
    observation_type: ObservationType = "generation"
    model: Optional[str] = None
    model_parameters: Optional[Dict[str, Any]] = None
    usage: Optional[Usage] = None
    cost: Optional[Cost] = None
    completion_start_time: Optional[float] = None
    prompt_name: Optional[str] = None
    prompt_version: Optional[str] = None


class ActiveChildHost:
    """Shared base for the two things you can open child spans under — a
    :class:`~darkhunt_telemetry.trace.Trace` (children nest under its root) and a
    :class:`Span` (children nest under it). Subclasses supply ``_tracer``,
    ``_trace_ref`` and ``_parent_context``; this base adds the ``span`` /
    ``generation`` / ``event`` factories and the ``start_active_*`` sugar once."""

    # --- implemented by subclasses ---
    @property
    def _tracer(self):  # pragma: no cover - overridden
        raise NotImplementedError

    @property
    def _trace_ref(self) -> "Trace":  # pragma: no cover - overridden
        raise NotImplementedError

    @property
    def _parent_context(self) -> Context:  # pragma: no cover - overridden
        raise NotImplementedError

    def span(
        self,
        name: str,
        *,
        input: Any = None,
        output: Any = None,
        metadata: Optional[Metadata] = None,
        level: Optional[ObservationLevel] = None,
        status_message: Optional[str] = None,
        version: Optional[str] = None,
        observation_type: ObservationType = "span",
        links: Optional[Sequence[Context]] = None,
        tool_name: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        tool_arguments: Any = None,
        start_time: Optional[float] = None,
    ) -> "Span":
        return Span(
            self._tracer,
            self._trace_ref,
            name,
            self._parent_context,
            _SpanOptions(
                input=input,
                output=output,
                metadata=metadata,
                level=level,
                status_message=status_message,
                version=version,
                observation_type=observation_type,
                links=links,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                tool_arguments=tool_arguments,
                start_time=start_time,
            ),
        )

    def generation(
        self,
        name: str,
        *,
        model: Optional[str] = None,
        model_parameters: Optional[Dict[str, Any]] = None,
        usage: Optional[Usage] = None,
        cost: Optional[Cost] = None,
        completion_start_time: Optional[float] = None,
        prompt_name: Optional[str] = None,
        prompt_version: Optional[str] = None,
        input: Any = None,
        output: Any = None,
        metadata: Optional[Metadata] = None,
        level: Optional[ObservationLevel] = None,
        status_message: Optional[str] = None,
        version: Optional[str] = None,
        links: Optional[Sequence[Context]] = None,
        tool_name: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        tool_arguments: Any = None,
        start_time: Optional[float] = None,
    ) -> "Generation":
        return Generation(
            self._tracer,
            self._trace_ref,
            name,
            self._parent_context,
            _GenerationOptions(
                input=input,
                output=output,
                metadata=metadata,
                level=level,
                status_message=status_message,
                version=version,
                links=links,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                tool_arguments=tool_arguments,
                start_time=start_time,
                model=model,
                model_parameters=model_parameters,
                usage=usage,
                cost=cost,
                completion_start_time=completion_start_time,
                prompt_name=prompt_name,
                prompt_version=prompt_version,
            ),
        )

    def event(self, name: str, **options: Any) -> None:
        """Fire-and-forget marker span (``observation_type='event'``); starts
        and ends immediately."""
        options.pop("observation_type", None)
        ev = self.span(name, observation_type="event", **options)
        ev.end()

    @contextmanager
    def start_active_span(self, name: str, **options: Any) -> Iterator["Span"]:
        """Open a child :class:`Span`, make it ACTIVE in the ambient OTel context
        for the duration of the ``with`` block, and end it on exit. Because the
        child is active, in-process spans opened without an explicit parent and
        third-party OTel auto-instrumentation nest under it, and it is the
        :func:`~darkhunt_telemetry.current_observation` for the block. On an exception the
        child is marked ERROR and the exception re-raised. Idempotent end — a
        body that ends the span itself is fully supported."""
        span = self.span(name, **options)
        token = context_api.attach(span.context)
        try:
            with use_observation(span):
                yield span
        except BaseException as err:
            span.end(level="ERROR", status_message=str(err))
            raise
        else:
            span.end()
        finally:
            context_api.detach(token)

    @contextmanager
    def start_active_generation(self, name: str, **options: Any) -> Iterator["Generation"]:
        """Active-context counterpart of :meth:`generation` — see
        :meth:`start_active_span`."""
        gen = self.generation(name, **options)
        token = context_api.attach(gen.context)
        try:
            with use_observation(gen):
                yield gen
        except BaseException as err:
            gen.end(level="ERROR", status_message=str(err))
            raise
        else:
            gen.end()
        finally:
            context_api.detach(token)


class Span(ActiveChildHost):
    def __init__(
        self,
        tracer,
        trace: "Trace",
        name: str,
        parent_context: Optional[Context],
        options: Optional[_SpanOptions] = None,
    ) -> None:
        self._tracer_obj = tracer
        self._trace_obj = trace
        parent_ctx = parent_context if parent_context is not None else context_api.get_current()
        opts = options or _SpanOptions()

        links = to_otel_links(opts.links)
        self._otel_span: OtelSpan = tracer.start_span(
            name,
            context=parent_ctx,
            links=links or None,
            start_time=_to_nanos(opts.start_time),
        )
        self._ctx = trace_api.set_span_in_context(self._otel_span, parent_ctx)
        self._ended = False
        self._writer = AttributeWriter(self._otel_span)

        self._observation_type = opts.observation_type or "span"
        self._tool_name = opts.tool_name
        self._otel_span.set_attribute(ATTR.OBSERVATION_TYPE, self._observation_type)
        self._apply_trace_attrs()
        if opts.input is not None:
            self._writer.set_io(ATTR.OBSERVATION_INPUT, opts.input)
        if opts.output is not None:
            self._writer.set_io(ATTR.OBSERVATION_OUTPUT, opts.output)
        if opts.metadata:
            self._writer.apply_metadata(opts.metadata)
        if opts.level:
            self._otel_span.set_attribute(ATTR.OBSERVATION_LEVEL, opts.level)
        self._writer.set_string(ATTR.STATUS_MESSAGE, opts.status_message)
        self._writer.set_string(ATTR.VERSION, opts.version)
        self._set_tool_attrs(opts.tool_name, opts.tool_call_id, opts.tool_arguments)

    # --- ActiveChildHost wiring ---
    @property
    def _tracer(self):
        return self._tracer_obj

    @property
    def _trace_ref(self) -> "Trace":
        return self._trace_obj

    @property
    def _parent_context(self) -> Context:
        return self._ctx

    # --- public accessors ---
    @property
    def context(self) -> Context:
        return self._ctx

    @property
    def trace(self) -> "Trace":
        return self._trace_obj

    @property
    def otel_span(self) -> OtelSpan:
        return self._otel_span

    @property
    def observation_type(self) -> str:
        return self._observation_type

    @property
    def tool_name(self) -> Optional[str]:
        return self._tool_name

    def handoff_token(self) -> HandoffToken:
        """A serializable :data:`HandoffToken` for THIS span. Hand it to a
        downstream agent's ``handoff_from`` to record a handoff from this span
        specifically (e.g. an orchestrator handing off from its ``dispatch``
        tool span)."""
        return span_context_to_token(self._otel_span.get_span_context())

    # --- context manager (lifecycle only) ---
    def __enter__(self) -> "Span":
        """Enter a ``with`` block that guarantees this span is ended on exit.

        NOTE: this only guarantees END; it does NOT make the span the ACTIVE
        OTel context. Ambient/auto-instrumented spans will NOT nest under it.
        Use :meth:`~darkhunt_telemetry.span.ActiveChildHost.start_active_span`
        when you also need the span to be the active context.
        """
        return self

    def __exit__(
        self,
        exc_type: "Optional[Type[BaseException]]",
        exc: "Optional[BaseException]",
        tb: "Optional[TracebackType]",
    ) -> Literal[False]:
        """End the span on ``with``-block exit. On an exception, mark the span
        ERROR with the exception message (mirroring ``start_active_span``);
        otherwise end normally. Never suppresses the exception. ``end()`` is
        idempotent, so a manual ``end()`` inside the block is fine."""
        if exc is not None:
            self.end(level="ERROR", status_message=str(exc))
        else:
            self.end()
        return False

    # --- mutation ---
    def update(
        self,
        *,
        name: Optional[str] = None,
        input: Any = None,
        output: Any = None,
        input_messages: Optional[Sequence[ChatMessage]] = None,
        output_messages: Optional[Sequence[ChatMessage]] = None,
        system_instructions: Optional[str] = None,
        metadata: Optional[Metadata] = None,
        level: Optional[ObservationLevel] = None,
        status_message: Optional[str] = None,
        version: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        tool_arguments: Any = None,
    ) -> "Span":
        if self._ended:
            warnings.warn(
                "darkhunt-telemetry: update() called on an already-ended span; ignored",
                stacklevel=2,
            )
            return self
        if name is not None:
            self._otel_span.update_name(name)
        if input is not None:
            self._writer.set_io(ATTR.OBSERVATION_INPUT, input)
        if output is not None:
            self._writer.set_io(ATTR.OBSERVATION_OUTPUT, output)
        self._writer.set_json(GEN_AI.INPUT_MESSAGES, input_messages)
        self._writer.set_json(GEN_AI.OUTPUT_MESSAGES, output_messages)
        if system_instructions is not None:
            self._otel_span.set_attribute(GEN_AI.SYSTEM_INSTRUCTIONS, system_instructions)
        if metadata:
            self._writer.apply_metadata(metadata)
        if level:
            self._otel_span.set_attribute(ATTR.OBSERVATION_LEVEL, level)
        self._writer.set_string(ATTR.STATUS_MESSAGE, status_message)
        self._writer.set_string(ATTR.VERSION, version)
        self._set_tool_attrs(tool_name, tool_call_id, tool_arguments)
        return self

    def end(
        self,
        *,
        output: Any = None,
        output_messages: Optional[Sequence[ChatMessage]] = None,
        status_message: Optional[str] = None,
        level: Optional[ObservationLevel] = None,
        end_time: Optional[float] = None,
    ) -> None:
        if self._ended:
            return
        self._ended = True

        if output is not None:
            self._writer.set_io(ATTR.OBSERVATION_OUTPUT, output)
        self._writer.set_json(GEN_AI.OUTPUT_MESSAGES, output_messages)
        self._writer.set_string(ATTR.STATUS_MESSAGE, status_message)
        if level:
            self._otel_span.set_attribute(ATTR.OBSERVATION_LEVEL, level)

        if level == "ERROR":
            self._otel_span.set_status(Status(StatusCode.ERROR, status_message or None))
        else:
            self._otel_span.set_status(Status(StatusCode.OK))

        self._otel_span.end(end_time=_to_nanos(end_time))

    # --- internals ---
    def _set_tool_attrs(
        self, tool_name: Optional[str], tool_call_id: Optional[str], tool_arguments: Any
    ) -> None:
        if tool_name is not None:
            self._tool_name = tool_name
        self._writer.set_string(GEN_AI.TOOL_NAME, tool_name)
        self._writer.set_string(GEN_AI.TOOL_CALL_ID, tool_call_id)
        if tool_arguments is not None:
            self._writer.set_io(GEN_AI.TOOL_CALL_ARGUMENTS, tool_arguments)

    def _apply_trace_attrs(self) -> None:
        t = self._trace_obj
        # Must be on EVERY span, not just the trace root: the backend resolves node
        # identity per span attribute set, so a root-only value would move the root
        # to the agent's node and strand its whole subtree on the Resource's node.
        if t.agent:
            self._otel_span.set_attribute(ATTR.SERVICE_NAME, t.agent)
        self._otel_span.set_attribute(ATTR.TENANT_ID, t.tenant_id)
        self._otel_span.set_attribute(ATTR.WORKSPACE_ID, t.workspace_id)
        self._otel_span.set_attribute(ATTR.APPLICATION_ID, t.application_id)
        self._otel_span.set_attribute(ATTR.ASSESSMENT_RUN_ID, t.assessment_run_id)
        if t.session_id:
            self._otel_span.set_attribute(ATTR.SESSION_ID, t.session_id)
        if t.user_id:
            self._otel_span.set_attribute(ATTR.USER_ID, t.user_id)
        if t.user_email:
            self._otel_span.set_attribute(ATTR.USER_EMAIL, t.user_email)
        if t.name:
            self._otel_span.set_attribute(ATTR.TRACE_NAME, t.name)
        # Trace-level context, also on every span. The root span is exported last — it ends
        # after its children — so children usually reach the backend in an earlier batch than
        # the root, and a root-only value never reaches them. Trace metadata goes first so the
        # span's own metadata, applied after this, wins on a shared key.
        if t._tags:
            self._otel_span.set_attribute(ATTR.TRACE_TAGS, ",".join(t._tags))
        if t._release:
            self._otel_span.set_attribute(ATTR.RELEASE, t._release)
        if t._environment:
            self._otel_span.set_attribute(ATTR.ENVIRONMENT, t._environment)
        if t._metadata:
            self._writer.apply_metadata(t._metadata)


class Generation(Span):
    def __init__(
        self,
        tracer,
        trace: "Trace",
        name: str,
        parent_context: Optional[Context],
        options: Optional[_GenerationOptions] = None,
    ) -> None:
        opts = options or _GenerationOptions()
        opts.observation_type = "generation"
        super().__init__(tracer, trace, name, parent_context, opts)

        self._apply_gen_attrs(
            model=opts.model,
            model_parameters=opts.model_parameters,
            usage=opts.usage,
            cost=opts.cost,
            completion_start_time=opts.completion_start_time,
            prompt_name=opts.prompt_name,
            prompt_version=opts.prompt_version,
        )

    def update(
        self,
        *,
        model: Optional[str] = None,
        model_parameters: Optional[Dict[str, Any]] = None,
        usage: Optional[Usage] = None,
        cost: Optional[Cost] = None,
        completion_start_time: Optional[float] = None,
        prompt_name: Optional[str] = None,
        prompt_version: Optional[str] = None,
        name: Optional[str] = None,
        input: Any = None,
        output: Any = None,
        input_messages: Optional[Sequence[ChatMessage]] = None,
        output_messages: Optional[Sequence[ChatMessage]] = None,
        system_instructions: Optional[str] = None,
        metadata: Optional[Metadata] = None,
        level: Optional[ObservationLevel] = None,
        status_message: Optional[str] = None,
        version: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        tool_arguments: Any = None,
    ) -> "Generation":
        super().update(
            name=name,
            input=input,
            output=output,
            input_messages=input_messages,
            output_messages=output_messages,
            system_instructions=system_instructions,
            metadata=metadata,
            level=level,
            status_message=status_message,
            version=version,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tool_arguments=tool_arguments,
        )
        if self._ended:
            return self
        self._apply_gen_attrs(
            model=model,
            model_parameters=model_parameters,
            usage=usage,
            cost=cost,
            completion_start_time=completion_start_time,
            prompt_name=prompt_name,
            prompt_version=prompt_version,
        )
        return self

    def end(
        self,
        *,
        model: Optional[str] = None,
        usage: Optional[Usage] = None,
        cost: Optional[Cost] = None,
        output: Any = None,
        output_messages: Optional[Sequence[ChatMessage]] = None,
        status_message: Optional[str] = None,
        level: Optional[ObservationLevel] = None,
        end_time: Optional[float] = None,
    ) -> None:
        # Skip the model/usage/cost setters when already ended — OTel logs a
        # warning per set_attribute on a dead span.
        if not self._ended:
            if model:
                self._set_model(model)
            if usage:
                self._set_usage(usage)
            if cost:
                self._set_cost(cost)
        super().end(
            output=output,
            output_messages=output_messages,
            status_message=status_message,
            level=level,
            end_time=end_time,
        )

    def _apply_gen_attrs(
        self,
        *,
        model: Optional[str],
        model_parameters: Optional[Dict[str, Any]],
        usage: Optional[Usage],
        cost: Optional[Cost],
        completion_start_time: Optional[float],
        prompt_name: Optional[str],
        prompt_version: Optional[str],
    ) -> None:
        """Shared body for the generation-specific attrs, used by both
        ``__init__`` and ``update`` so the model/usage/cost/prompt logic lives
        once."""
        if model:
            self._set_model(model)
        self._writer.set_json(ATTR.MODEL_PARAMETERS, model_parameters)
        if usage:
            self._set_usage(usage)
        if cost:
            self._set_cost(cost)
        if completion_start_time is not None:
            self._otel_span.set_attribute(
                ATTR.COMPLETION_START_TIME,
                int(math.floor(completion_start_time * 1e9)),
            )
        self._writer.set_string(ATTR.PROMPT_NAME, prompt_name)
        self._writer.set_string(ATTR.PROMPT_VERSION, prompt_version)

    def _set_model(self, model: str) -> None:
        self._otel_span.set_attribute(ATTR.MODEL_NAME, model)
        self._otel_span.set_attribute(GEN_AI.REQUEST_MODEL, model)

    def _set_usage(self, usage: Usage) -> None:
        self._otel_span.set_attribute(ATTR.USAGE_DETAILS, safe_json_dumps(usage))
        if usage.get("input_tokens") is not None:
            self._otel_span.set_attribute(GEN_AI.USAGE_INPUT_TOKENS, usage["input_tokens"])
        if usage.get("output_tokens") is not None:
            self._otel_span.set_attribute(GEN_AI.USAGE_OUTPUT_TOKENS, usage["output_tokens"])
        if usage.get("cache_read_tokens") is not None:
            self._otel_span.set_attribute(
                GEN_AI.USAGE_CACHE_READ_INPUT_TOKENS, usage["cache_read_tokens"]
            )
        if usage.get("cache_creation_tokens") is not None:
            self._otel_span.set_attribute(
                GEN_AI.USAGE_CACHE_CREATION_INPUT_TOKENS, usage["cache_creation_tokens"]
            )

    def _set_cost(self, cost: Cost) -> None:
        self._otel_span.set_attribute(ATTR.COST_DETAILS, safe_json_dumps(cost))
        if cost.get("total") is not None:
            self._otel_span.set_attribute(GEN_AI.USAGE_COST, cost["total"])


def token_to_context(token: HandoffToken) -> Optional[Context]:
    """Parse a :data:`HandoffToken` back into an OTel context carrying its span
    context — via the global propagator, with a direct-parse fallback."""
    ctx = _PROPAGATOR.extract({"traceparent": token})
    sc = trace_api.get_current_span(ctx).get_span_context()
    if sc.is_valid:
        return ctx
    parts = token.split("-")
    if len(parts) < 4:
        return None
    _, trace_id, span_id, flags = parts[0], parts[1], parts[2], parts[3]
    if not trace_id or not span_id:
        return None
    try:
        sc2 = SpanContext(
            trace_id=int(trace_id, 16),
            span_id=int(span_id, 16),
            is_remote=True,
            trace_flags=TraceFlags(int(flags, 16) or 1),
        )
    except ValueError:
        return None
    if not sc2.is_valid:
        return None
    return trace_api.set_span_in_context(NonRecordingSpan(sc2))


__all__ = [
    "Span",
    "Generation",
    "ActiveChildHost",
    "AttributeWriter",
    "ChatMessage",
    "HandoffToken",
    "LINK_KIND_ATTR",
    "HANDOFF_LINK_KIND",
    "span_context_to_token",
    "token_to_context",
    "to_otel_links",
    "apply_metadata_attrs",
]
