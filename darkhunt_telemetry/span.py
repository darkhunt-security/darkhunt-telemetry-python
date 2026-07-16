"""Span + Generation — port of ``src/span.ts``.

A :class:`Span` wraps an OTel span and applies the Darkhunt attribute schema
(routing attrs, masked input/output, metadata, tool fields). A
:class:`Generation` is a Span specialized for LLM round-trips (model / usage /
cost). Both are created through a :class:`~darkhunt_telemetry.trace.Trace` (or a
parent Span) so masking + routing context flow down automatically.
"""

from __future__ import annotations

import math
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional, Sequence

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

from .attributes import ATTR, GEN_AI
from .masking import safe_json_dumps
from .types import ChatMessage, Cost, Metadata, ObservationLevel, ObservationType, Usage

if TYPE_CHECKING:  # avoid a runtime import cycle with trace.py
    from .masking import Sanitizer
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


def apply_metadata_attrs(
    span: OtelSpan, metadata: Metadata, sanitizer: "Optional[Sanitizer]"
) -> None:
    for k, v in metadata.items():
        if v is None:
            continue
        # Keys land in the OTel attribute name verbatim; mask them too.
        if sanitizer is not None and isinstance(k, str):
            safe_key = sanitizer.sanitize(k)
        else:
            safe_key = str(k)
        key = f"{ATTR.METADATA_PREFIX}{safe_key}"
        value = sanitizer.sanitize_unknown(v) if sanitizer is not None else v
        if isinstance(value, (str, int, float)):  # bool is an int subclass
            span.set_attribute(key, value)
        else:
            span.set_attribute(key, safe_json_dumps(value))


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
        third-party OTel auto-instrumentation nest under it. On an exception the
        child is marked ERROR and the exception re-raised. Idempotent end — a
        body that ends the span itself is fully supported."""
        span = self.span(name, **options)
        token = context_api.attach(span.context)
        try:
            yield span
        except BaseException as err:
            span.end(level="ERROR", status_message=str(err))
            raise
        else:
            span.end()
        finally:
            context_api.detach(token)

    @contextmanager
    def start_active_generation(
        self, name: str, **options: Any
    ) -> Iterator["Generation"]:
        """Active-context counterpart of :meth:`generation` — see
        :meth:`start_active_span`."""
        gen = self.generation(name, **options)
        token = context_api.attach(gen.context)
        try:
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
        # Span name lands on the wire verbatim — mask in case user-controlled.
        self._otel_span: OtelSpan = tracer.start_span(
            trace.mask_name(name),
            context=parent_ctx,
            links=links or None,
            start_time=_to_nanos(opts.start_time),
        )
        self._ctx = trace_api.set_span_in_context(self._otel_span, parent_ctx)
        self._ended = False

        self._otel_span.set_attribute(ATTR.OBSERVATION_TYPE, opts.observation_type or "span")
        self._apply_trace_attrs()
        if opts.input is not None:
            self._set_io(ATTR.OBSERVATION_INPUT, opts.input)
        if opts.output is not None:
            self._set_io(ATTR.OBSERVATION_OUTPUT, opts.output)
        if opts.metadata:
            apply_metadata_attrs(self._otel_span, opts.metadata, self._trace_obj.sanitizer)
        if opts.level:
            self._otel_span.set_attribute(ATTR.OBSERVATION_LEVEL, opts.level)
        self._set_masked_string_attr(ATTR.STATUS_MESSAGE, opts.status_message)
        self._set_masked_string_attr(ATTR.VERSION, opts.version)
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

    def handoff_token(self) -> HandoffToken:
        """A serializable :data:`HandoffToken` for THIS span. Hand it to a
        downstream agent's ``handoff_from`` to record a handoff from this span
        specifically (e.g. an orchestrator handing off from its ``dispatch``
        tool span)."""
        return span_context_to_token(self._otel_span.get_span_context())

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
            self._otel_span.update_name(self._trace_obj.mask_name(name))
        if input is not None:
            self._set_io(ATTR.OBSERVATION_INPUT, input)
        if output is not None:
            self._set_io(ATTR.OBSERVATION_OUTPUT, output)
        self._set_masked_json_attr(GEN_AI.INPUT_MESSAGES, input_messages)
        self._set_masked_json_attr(GEN_AI.OUTPUT_MESSAGES, output_messages)
        if system_instructions is not None:
            self._otel_span.set_attribute(
                GEN_AI.SYSTEM_INSTRUCTIONS, self._mask_string(system_instructions)
            )
        if metadata:
            apply_metadata_attrs(self._otel_span, metadata, self._trace_obj.sanitizer)
        if level:
            self._otel_span.set_attribute(ATTR.OBSERVATION_LEVEL, level)
        self._set_masked_string_attr(ATTR.STATUS_MESSAGE, status_message)
        self._set_masked_string_attr(ATTR.VERSION, version)
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
            self._set_io(ATTR.OBSERVATION_OUTPUT, output)
        self._set_masked_json_attr(GEN_AI.OUTPUT_MESSAGES, output_messages)
        masked_status = self._mask_string(status_message) if status_message else None
        if masked_status is not None:
            self._otel_span.set_attribute(ATTR.STATUS_MESSAGE, masked_status)
        if level:
            self._otel_span.set_attribute(ATTR.OBSERVATION_LEVEL, level)

        if level == "ERROR":
            self._otel_span.set_status(Status(StatusCode.ERROR, masked_status))
        else:
            self._otel_span.set_status(Status(StatusCode.OK))

        self._otel_span.end(end_time=_to_nanos(end_time))

    # --- internals ---
    def _set_io(self, key: str, value: Any) -> None:
        if value is None:
            return
        sanitizer = self._trace_obj.sanitizer
        sanitized = sanitizer.sanitize_unknown(value) if sanitizer is not None else value
        if isinstance(sanitized, str):
            self._otel_span.set_attribute(key, sanitized)
        else:
            self._otel_span.set_attribute(key, safe_json_dumps(sanitized))

    def _mask_string(self, value: str) -> str:
        sanitizer = self._trace_obj.sanitizer
        return sanitizer.sanitize(value) if sanitizer is not None else value

    def _set_masked_string_attr(self, key: str, value: Optional[str]) -> None:
        if value:
            self._otel_span.set_attribute(key, self._mask_string(value))

    def _set_tool_attrs(
        self, tool_name: Optional[str], tool_call_id: Optional[str], tool_arguments: Any
    ) -> None:
        self._set_masked_string_attr(GEN_AI.TOOL_NAME, tool_name)
        self._set_masked_string_attr(GEN_AI.TOOL_CALL_ID, tool_call_id)
        if tool_arguments is not None:
            self._set_io(GEN_AI.TOOL_CALL_ARGUMENTS, tool_arguments)

    def _set_masked_json_attr(self, key: str, value: Any) -> None:
        if value is None:
            return
        sanitizer = self._trace_obj.sanitizer
        masked = sanitizer.sanitize_unknown(value) if sanitizer is not None else value
        self._otel_span.set_attribute(key, safe_json_dumps(masked))

    def _apply_trace_attrs(self) -> None:
        t = self._trace_obj
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
            self._otel_span.set_attribute(ATTR.TRACE_NAME, t.mask_name(t.name))


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

        if opts.model:
            self._set_model(opts.model)
        # Walk modelParameters: operators sometimes tuck provider keys or webhook
        # URLs in here for custom backends.
        self._set_masked_json_attr(ATTR.MODEL_PARAMETERS, opts.model_parameters)
        if opts.usage:
            self._set_usage(opts.usage)
        if opts.cost:
            self._set_cost(opts.cost)
        if opts.completion_start_time is not None:
            self._otel_span.set_attribute(
                ATTR.COMPLETION_START_TIME,
                int(math.floor(opts.completion_start_time * 1e9)),
            )
        self._set_masked_string_attr(ATTR.PROMPT_NAME, opts.prompt_name)
        self._set_masked_string_attr(ATTR.PROMPT_VERSION, opts.prompt_version)

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
        if model:
            self._set_model(model)
        self._set_masked_json_attr(ATTR.MODEL_PARAMETERS, model_parameters)
        if usage:
            self._set_usage(usage)
        if cost:
            self._set_cost(cost)
        if completion_start_time is not None:
            self._otel_span.set_attribute(
                ATTR.COMPLETION_START_TIME, int(math.floor(completion_start_time * 1e9))
            )
        self._set_masked_string_attr(ATTR.PROMPT_NAME, prompt_name)
        self._set_masked_string_attr(ATTR.PROMPT_VERSION, prompt_version)
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
    "ChatMessage",
    "HandoffToken",
    "LINK_KIND_ATTR",
    "HANDOFF_LINK_KIND",
    "span_context_to_token",
    "token_to_context",
    "to_otel_links",
    "apply_metadata_attrs",
]
