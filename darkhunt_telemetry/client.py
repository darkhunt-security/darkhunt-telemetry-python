"""DarkhuntTelemetry client — port of ``src/client.ts``.

Construct one per process (lifetime-of-the-process). It builds a private OTel
:class:`TracerProvider` + :class:`BatchSpanProcessor` feeding the Darkhunt OTLP
exporter, resolves configuration from options/env, and is the factory for
:class:`~darkhunt_telemetry.trace.Trace` objects.
"""

from __future__ import annotations

import atexit
import os
import warnings
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence, Set

from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Tracer

from ._version import __version__ as LIB_VERSION
from .exporter import DarkhuntSpanExporter, ExporterStats, TelemetryEvent
from .masking import CustomPattern, Sanitizer
from .otel_globals import register_otel_context_globals
from .trace import Trace

LIB_NAME = "darkhunt-telemetry"

# Single shared atexit handler across all SDK instances (mirrors the TS SDK's
# shared beforeExit handler). Per-instance handlers would leak.
_active_instances: "Set[DarkhuntTelemetry]" = set()
_atexit_installed = False


def _ensure_atexit_handler() -> None:
    global _atexit_installed
    if _atexit_installed:
        return
    _atexit_installed = True
    atexit.register(_atexit_handler)


def _atexit_handler() -> None:
    # Iterate a snapshot: shutdown() mutates the live set.
    for dh in tuple(_active_instances):
        try:
            dh.shutdown()
        except Exception:  # nosec B110 - shutdown already swallows  # pragma: no cover
            pass


@dataclass
class MaskingOptions:
    """Client-side data masking configuration.

    - ``enabled``: mask inputs/outputs/messages/system prompts/metadata/status
      messages before they leave the process. Defaults True.
    - ``custom_patterns``: operator-defined extra rules merged after the bundled
      defaults (site-specific patterns like internal ticket IDs).
    """

    enabled: bool = True
    custom_patterns: Sequence[CustomPattern] = field(default_factory=list)


def _env(name: str) -> Optional[str]:
    return os.environ.get(name)


def _to_int(value: Optional[str], fallback: int) -> int:
    if value is None:
        return fallback
    try:
        return int(value)
    except (ValueError, TypeError):
        return fallback


def _to_float(value: Optional[str], fallback: float) -> float:
    if value is None:
        return fallback
    try:
        return float(value)
    except (ValueError, TypeError):
        return fallback


class DarkhuntTelemetry:
    """The Darkhunt telemetry client. One per process.

    Configuration resolves ``constructor arg > env var > default`` for each
    field. See the README for the full env-var table.
    """

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        service_name: Optional[str] = None,
        flush_at: Optional[int] = None,
        flush_interval_ms: Optional[float] = None,
        timeout_ms: Optional[float] = None,
        release: Optional[str] = None,
        environment: Optional[str] = None,
        enabled: Optional[bool] = None,
        internal: Optional[bool] = None,
        mask: Optional[MaskingOptions] = None,
        register_context_manager: Optional[bool] = None,
        tenant_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        application_id: Optional[str] = None,
        assessment_run_id: Optional[str] = None,
        on_error: Optional[Callable[[TelemetryEvent], None]] = None,
    ) -> None:
        # Ingest host, not the dashboard host (which redirects POSTs -> 405).
        base_url = base_url or _env("DARKHUNT_BASE_URL") or "https://api.darkhunt.ai/trace-hub"
        api_key = api_key if api_key is not None else (_env("DARKHUNT_API_KEY") or "")
        self._release = release if release is not None else _env("DARKHUNT_RELEASE")
        self._environment = environment if environment is not None else _env("DARKHUNT_ENVIRONMENT")
        self._tenant_id = tenant_id if tenant_id is not None else _env("DARKHUNT_TENANT_ID")
        self._workspace_id = (
            workspace_id if workspace_id is not None else _env("DARKHUNT_WORKSPACE_ID")
        )
        self._application_id = (
            application_id if application_id is not None else _env("DARKHUNT_APPLICATION_ID")
        )
        self._assessment_run_id = (
            assessment_run_id
            if assessment_run_id is not None
            else _env("DARKHUNT_ASSESSMENT_RUN_ID")
        )

        enabled_env = (_env("DARKHUNT_ENABLED") or "true").lower() == "true"
        self._enabled = enabled if enabled is not None else enabled_env

        internal_resolved = (
            internal
            if internal is not None
            else (_env("DARKHUNT_INTERNAL") or "false").lower() == "true"
        )

        # Internal endpoint is permitAll; no apiKey needed. Public requires one.
        if self._enabled and not internal_resolved and not api_key:
            raise ValueError(
                "DarkhuntTelemetry: api_key is required for the public endpoint "
                "(pass via options, set DARKHUNT_API_KEY, or use internal=True)"
            )

        masking_enabled = mask.enabled if mask is not None else True
        self._sanitizer: Optional[Sanitizer] = None
        if self._enabled and masking_enabled:
            custom = list(mask.custom_patterns) if mask is not None else []
            self._sanitizer = Sanitizer(custom_patterns=custom)

        # ``or`` (not a None-check) so an empty-string env var falls through to
        # the next source instead of producing an empty service.name.
        resolved_service_name = (
            service_name or _env("DARKHUNT_SERVICE_NAME") or _env("OTEL_SERVICE_NAME") or LIB_NAME
        )

        self._on_error = on_error
        self._provider: Optional[TracerProvider] = None
        self._tracer: Optional[Tracer] = None
        self._exporter: Optional[DarkhuntSpanExporter] = None

        if self._enabled:
            register_ctx = (
                register_context_manager
                if register_context_manager is not None
                else (_env("DARKHUNT_REGISTER_CONTEXT_MANAGER") or "true").lower() != "false"
            )
            if register_ctx:
                register_otel_context_globals()

            self._setup_provider(
                base_url=base_url,
                api_key=api_key,
                internal=internal_resolved,
                service_name=resolved_service_name,
                flush_at=(
                    flush_at if flush_at is not None else _to_int(_env("DARKHUNT_FLUSH_AT"), 20)
                ),
                flush_interval_ms=(
                    flush_interval_ms
                    if flush_interval_ms is not None
                    else _to_float(_env("DARKHUNT_FLUSH_INTERVAL"), 5) * 1000
                ),
                timeout_ms=(
                    timeout_ms
                    if timeout_ms is not None
                    else _to_float(_env("DARKHUNT_TIMEOUT"), 10) * 1000
                ),
            )
            _active_instances.add(self)
            _ensure_atexit_handler()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def stats(self) -> Optional[ExporterStats]:
        """Delivery counters (spans exported / dropped-unroutable / export
        failures), or ``None`` when telemetry is disabled and no exporter
        exists."""
        if self._exporter is None:
            return None
        return self._exporter.stats()

    def trace(
        self,
        name: Optional[str] = None,
        *,
        tenant_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        application_id: Optional[str] = None,
        assessment_run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        user_email: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
        metadata: Optional[dict] = None,
        release: Optional[str] = None,
        environment: Optional[str] = None,
        links: Optional[Sequence[Any]] = None,
        handoff_from: Optional[Sequence[Any]] = None,
        observation_type: str = "agent",
        input: Any = None,
        output: Any = None,
        start_time: Optional[float] = None,
    ) -> Trace:
        """Open a new trace. Routing fields merge ``per-call > constructor
        default > env``; raises :class:`ValueError` if tenant/workspace/application
        is still missing."""
        merged_tenant = tenant_id if tenant_id is not None else self._tenant_id
        merged_workspace = workspace_id if workspace_id is not None else self._workspace_id
        merged_application = application_id if application_id is not None else self._application_id
        merged_assessment = (
            assessment_run_id if assessment_run_id is not None else self._assessment_run_id
        )
        merged_release = release if release is not None else self._release
        merged_environment = environment if environment is not None else self._environment

        _require_field(merged_tenant, "tenant_id", "DARKHUNT_TENANT_ID")
        _require_field(merged_workspace, "workspace_id", "DARKHUNT_WORKSPACE_ID")
        _require_field(merged_application, "application_id", "DARKHUNT_APPLICATION_ID")
        # assessment_run_id is optional — used internally by Darkhunt assessment
        # workflows. Production tracing does not need to set it.

        if self._enabled and self._tracer is not None:
            tracer = self._tracer
        else:
            from opentelemetry import trace as trace_api

            tracer = trace_api.get_tracer(LIB_NAME, LIB_VERSION)

        return Trace(
            tracer,
            name=name,
            tenant_id=merged_tenant,
            workspace_id=merged_workspace,
            application_id=merged_application,
            assessment_run_id=merged_assessment,
            session_id=session_id,
            user_id=user_id,
            user_email=user_email,
            tags=tags,
            metadata=metadata,
            release=merged_release,
            environment=merged_environment,
            links=links,
            handoff_from=handoff_from,
            observation_type=observation_type,  # type: ignore[arg-type]
            input=input,
            output=output,
            start_time=start_time,
            sanitizer=self._sanitizer,
        )

    def flush(self) -> bool:
        """Force-flush pending spans. Safe to call any time.

        Returns True if the flush completed (or there was nothing to flush) and
        False if it failed or timed out. Delivery-level failures are surfaced via
        the ``on_error`` hook and :meth:`stats`; a False return here reflects the
        provider-level force-flush result. Backward compatible: callers ignoring
        the return value are unaffected."""
        if self._provider is None:
            return True
        try:
            return bool(self._provider.force_flush())
        except Exception as err:  # pragma: no cover - defensive
            warnings.warn(
                f"darkhunt-telemetry: force_flush() failed; spans may be lost: {err}",
                stacklevel=2,
            )
            return False

    def shutdown(self) -> None:
        """Flush and tear down the provider. Idempotent."""
        _active_instances.discard(self)
        if self._provider is not None:
            try:
                self._provider.shutdown()
            except Exception as err:  # pragma: no cover - defensive
                warnings.warn(
                    f"darkhunt-telemetry: provider.shutdown() failed: {err}", stacklevel=2
                )
            self._provider = None
            self._tracer = None

    def _setup_provider(
        self,
        *,
        base_url: str,
        api_key: str,
        internal: bool,
        service_name: str,
        flush_at: int,
        flush_interval_ms: float,
        timeout_ms: float,
    ) -> None:
        resource = Resource.create({SERVICE_NAME: service_name, SERVICE_VERSION: LIB_VERSION})
        exporter = DarkhuntSpanExporter(
            base_url=base_url,
            api_key=api_key,
            timeout_ms=timeout_ms,
            internal=internal,
            on_error=self._on_error,
        )
        self._exporter = exporter
        # Manage teardown ourselves via the shared atexit handler (mirrors the TS
        # single shared beforeExit handler), so disable the provider's own.
        self._provider = TracerProvider(resource=resource, shutdown_on_exit=False)
        self._provider.add_span_processor(
            BatchSpanProcessor(
                exporter,
                max_export_batch_size=flush_at,
                schedule_delay_millis=flush_interval_ms,
            )
        )
        self._tracer = self._provider.get_tracer(LIB_NAME, LIB_VERSION)


def _require_field(value: Optional[str], option_name: str, env_var: str) -> None:
    if not value:
        raise ValueError(
            f"DarkhuntTelemetry: {option_name} is required. Pass it on "
            f"dh.trace({option_name}=...), set it as a default on "
            f"DarkhuntTelemetry({option_name}=...), or set the {env_var} env var."
        )


__all__ = ["DarkhuntTelemetry", "MaskingOptions", "TelemetryEvent", "ExporterStats"]
