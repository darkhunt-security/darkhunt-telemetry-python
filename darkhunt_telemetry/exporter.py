"""Darkhunt OTLP span exporter — port of ``src/exporter.ts``.

Groups spans by their (tenant, workspace, application, assessmentRun) routing
tuple, serializes each group to OTLP/protobuf, and POSTs it to that tenant's
scoped ingest endpoint with bounded retry + jittered backoff.
"""

from __future__ import annotations

import secrets
import threading
import time
import warnings
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Literal, Optional, Sequence, Tuple

import requests
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from .attributes import ATTR

_MAX_RETRIES = 3
_INITIAL_BACKOFF_MS = 1000
_MAX_BACKOFF_MS = 30_000
_BACKOFF_MULTIPLIER = 2
_RETRYABLE_STATUS = frozenset({429, 502, 503, 504})
# Number of slices each backoff sleep is chopped into. Sleeping in slices (with
# a shutdown check between each) lets ``shutdown()`` interrupt a retry promptly
# without blocking the single BatchSpanProcessor worker for the full backoff,
# while still honouring a monkeypatched ``time.sleep`` in tests.
_RETRY_SLEEP_SLICES = 20


@dataclass(frozen=True)
class _RouteKey:
    tenant_id: str
    workspace_id: str
    application_id: str
    assessment_run_id: str


@dataclass(frozen=True)
class TelemetryEvent:
    """A best-effort delivery-observability event handed to the ``on_error``
    hook. Immutable so a misbehaving hook cannot mutate exporter state.

    - ``kind`` — ``"export_failed"`` when a route-group's POST failed after
      retries were exhausted (or was interrupted by shutdown); ``"spans_dropped"``
      when a span was discarded for missing routing attributes.
    - ``span_count`` / ``group_count`` — how many spans / route-groups the event
      covers.
    - ``http_status`` — the last HTTP status seen (``None`` for a network error
      or a drop).
    - ``error`` — ``repr`` of the last exception, or a short reason string.
    - routing fields — present on ``export_failed`` events.
    - ``missing_attributes`` — the routing attribute names that were absent, on
      ``spans_dropped`` events.
    """

    kind: Literal["export_failed", "spans_dropped"]
    span_count: int
    group_count: int = 0
    http_status: Optional[int] = None
    error: Optional[str] = None
    tenant_id: Optional[str] = None
    workspace_id: Optional[str] = None
    application_id: Optional[str] = None
    assessment_run_id: Optional[str] = None
    missing_attributes: Tuple[str, ...] = field(default_factory=tuple)


TelemetryEventHook = Callable[[TelemetryEvent], None]


@dataclass(frozen=True)
class ExporterStats:
    """A point-in-time snapshot of exporter delivery counters."""

    spans_exported: int
    spans_dropped_unroutable: int
    export_failures: int


class DarkhuntSpanExporter(SpanExporter):
    """A :class:`SpanExporter` that routes each span to its tenant-scoped
    trace-hub ingest endpoint.

    When ``internal`` is True, posts to ``/internal/t/{tenantId}/v1/traces``
    (permitAll, no auth) instead of the public ``/otlp/...`` path — for
    in-cluster service-to-service traffic where the upstream auth header is
    not present.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_ms: float,
        internal: bool = False,
        session: Optional[requests.Session] = None,
        on_error: Optional[TelemetryEventHook] = None,
    ) -> None:
        self._base_url = _strip_trailing_slashes(base_url)
        self._api_key = api_key
        self._timeout_s = timeout_ms / 1000.0
        self._internal = internal
        self._on_error = on_error
        # Set by shutdown() to interrupt an in-flight retry backoff promptly.
        self._shutdown_event = threading.Event()
        self._session = session or requests.Session()
        # Dedupe drop warnings — log once per (missing-fields, span-name) pair.
        self._dropped_warned: set = set()
        # Delivery counters, guarded for cross-thread reads via stats().
        self._stats_lock = threading.Lock()
        self._spans_exported = 0
        self._spans_dropped = 0
        self._export_failures = 0

    @property
    def _shutdown_called(self) -> bool:
        return self._shutdown_event.is_set()

    def stats(self) -> ExporterStats:
        """Return a snapshot of the delivery counters. Safe to call any time,
        from any thread."""
        with self._stats_lock:
            return ExporterStats(
                spans_exported=self._spans_exported,
                spans_dropped_unroutable=self._spans_dropped,
                export_failures=self._export_failures,
            )

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self._shutdown_event.is_set():
            return SpanExportResult.FAILURE
        try:
            return self._export(spans)
        except Exception:  # pragma: no cover - defensive; never raise from export
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        self._shutdown_event.set()
        try:
            self._session.close()
        except Exception:  # nosec B110 - best-effort close; nothing to recover  # pragma: no cover
            pass

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """No-op: this exporter has no internal buffer. The BatchSpanProcessor
        upstream already calls :meth:`export` synchronously when it flushes."""
        return True

    def _export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        groups = self._group_by_route(spans)
        if not groups:
            return SpanExportResult.SUCCESS

        failed = False
        for route, group in groups.values():
            if not self._export_group(route, group):
                failed = True
        return SpanExportResult.FAILURE if failed else SpanExportResult.SUCCESS

    def _group_by_route(self, spans: Sequence[ReadableSpan]) -> Dict[str, tuple]:
        groups: Dict[str, tuple] = {}
        for span in spans:
            route = self._extract_route(span)
            if route is None:
                continue
            key = (
                f"{route.tenant_id}|{route.workspace_id}|"
                f"{route.application_id}|{route.assessment_run_id}"
            )
            entry = groups.get(key)
            if entry is None:
                groups[key] = (route, [span])
            else:
                entry[1].append(span)
        return groups

    def _extract_route(self, span: ReadableSpan) -> Optional[_RouteKey]:
        a = span.attributes or {}
        tenant_id = _string_attr(a.get(ATTR.TENANT_ID))
        workspace_id = _string_attr(a.get(ATTR.WORKSPACE_ID))
        application_id = _string_attr(a.get(ATTR.APPLICATION_ID))
        assessment_run_id = _string_attr(a.get(ATTR.ASSESSMENT_RUN_ID))
        if tenant_id and workspace_id and application_id:
            return _RouteKey(tenant_id, workspace_id, application_id, assessment_run_id)
        missing = []
        if not tenant_id:
            missing.append("tenantId")
        if not workspace_id:
            missing.append("workspaceId")
        if not application_id:
            missing.append("applicationId")
        with self._stats_lock:
            self._spans_dropped += 1
        self._warn_dropped_span(span.name, missing)
        self._emit(
            TelemetryEvent(
                kind="spans_dropped",
                span_count=1,
                missing_attributes=tuple(missing),
            )
        )
        return None

    def _warn_dropped_span(self, span_name: str, missing: List[str]) -> None:
        key = f"{span_name}::{','.join(missing)}"
        if key in self._dropped_warned:
            return
        self._dropped_warned.add(key)
        warnings.warn(
            f'DarkhuntSpanExporter: dropping span "{span_name}" — missing required '
            f"routing attribute(s): {', '.join(missing)}. The exporter requires "
            f"tenantId, workspaceId, and applicationId on every trace; spans without "
            f"all three cannot be routed and are silently discarded. Verify the "
            f"caller passed them to client.trace(...).",
            stacklevel=2,
        )

    def _export_group(self, route: _RouteKey, spans: List[ReadableSpan]) -> bool:
        request = encode_spans(spans)
        body = request.SerializeToString()
        if not body:
            return True
        ok, status, error = self._send_with_retry(self._build_url(route), body, route)
        if ok:
            with self._stats_lock:
                self._spans_exported += len(spans)
            return True
        with self._stats_lock:
            self._export_failures += 1
        self._emit(
            TelemetryEvent(
                kind="export_failed",
                span_count=len(spans),
                group_count=1,
                http_status=status,
                error=error,
                tenant_id=route.tenant_id,
                workspace_id=route.workspace_id,
                application_id=route.application_id,
                assessment_run_id=route.assessment_run_id,
            )
        )
        return False

    def _build_url(self, route: _RouteKey) -> str:
        path_prefix = "internal" if self._internal else "otlp"
        return f"{self._base_url}/{path_prefix}/t/{_url_quote(route.tenant_id)}/v1/traces"

    def _send_with_retry(
        self, url: str, body: bytes, route: _RouteKey
    ) -> Tuple[bool, Optional[int], Optional[str]]:
        """POST ``body`` with bounded, interruptible retry.

        Returns ``(success, last_http_status, last_error)``. Cumulative backoff
        is capped at the configured export timeout so a single group's retries
        cannot outlast the flush/shutdown window, and each backoff aborts
        promptly if :meth:`shutdown` is called mid-retry.
        """
        headers = {
            "Content-Type": "application/x-protobuf",
            "X-Workspace-Id": route.workspace_id,
            "X-Application-Id": route.application_id,
        }
        # Internal endpoint is permitAll; skip the bearer so we don't attach a
        # stale/empty token to in-cluster requests.
        if not self._internal:
            headers["Authorization"] = f"Bearer {self._api_key}"

        # Total backoff budget: never sleep longer, cumulatively, than the export
        # timeout — otherwise a retrying group would block the batch worker (and
        # shutdown) far past the timeout the operator configured.
        budget_s = self._timeout_s
        slept_s = 0.0
        last_status: Optional[int] = None
        last_error: Optional[str] = None
        backoff = _INITIAL_BACKOFF_MS
        for attempt in range(_MAX_RETRIES):
            if self._shutdown_event.is_set():
                return False, last_status, last_error or "shutdown requested"
            outcome, status, error = self._post_once(url, headers, body)
            if status is not None:
                last_status = status
            if error is not None:
                last_error = error
            if outcome is not None:
                return outcome, last_status, None
            waited = self._wait_before_retry(attempt, backoff, budget_s - slept_s)
            if waited is None:
                break
            slept_s += waited
            backoff = min(backoff * _BACKOFF_MULTIPLIER, _MAX_BACKOFF_MS)
        if self._shutdown_event.is_set():
            # Shutdown fired mid-backoff: stop retrying, report failure.
            return False, last_status, "shutdown requested during retry"
        return False, last_status, last_error

    def _wait_before_retry(self, attempt: int, backoff: int, remaining_s: float) -> Optional[float]:
        """Back off before the next retry attempt.

        Returns the seconds slept, or ``None`` when retrying must stop: the
        final attempt was just made, the cumulative backoff budget is spent,
        or shutdown fired mid-backoff.
        """
        if attempt == _MAX_RETRIES - 1:
            return None
        if remaining_s <= 0:
            return None
        # Add 0–50% jitter so concurrent retrying clients don't synchronize.
        # secrets (not random) to satisfy strict analyzers — jitter has no
        # security impact.
        jitter_ms = secrets.randbelow(max(1, backoff // 2))
        sleep_s = min((backoff + jitter_ms) / 1000.0, remaining_s)
        if self._sleep_for_retry(sleep_s):
            return None
        return sleep_s

    def _post_once(
        self, url: str, headers: Dict[str, str], body: bytes
    ) -> Tuple[Optional[bool], Optional[int], Optional[str]]:
        """Make one POST attempt and classify the result.

        Returns ``(outcome, status, error)``. ``outcome`` is True on success,
        False on a non-retryable response, and None when the caller should
        retry (network error or a retryable status).
        """
        try:
            resp = self._session.post(url, headers=headers, data=body, timeout=self._timeout_s)
        except requests.RequestException as err:
            # network/timeout — retry
            return None, None, repr(err)
        if resp.ok:
            return True, resp.status_code, None
        if resp.status_code in _RETRYABLE_STATUS:
            return None, resp.status_code, None
        return False, resp.status_code, None

    def _sleep_for_retry(self, seconds: float) -> bool:
        """Sleep ~``seconds`` in slices, returning True if shutdown was requested
        mid-sleep (the caller should then stop retrying)."""
        if self._shutdown_event.is_set():
            return True
        if seconds <= 0:
            return False
        slice_s = seconds / _RETRY_SLEEP_SLICES
        for _ in range(_RETRY_SLEEP_SLICES):
            if self._shutdown_event.is_set():
                return True
            time.sleep(slice_s)
        return self._shutdown_event.is_set()

    def _emit(self, event: TelemetryEvent) -> None:
        """Invoke the observability hook best-effort. A throwing hook must never
        break export, so all exceptions are swallowed."""
        hook = self._on_error
        if hook is None:
            return
        try:
            hook(event)
        except Exception:  # nosec B110 - hook is best-effort; never break export
            pass


def _string_attr(value: object) -> str:
    return value if isinstance(value, str) else ""


def _strip_trailing_slashes(url: str) -> str:
    return url.rstrip("/")


def _url_quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


__all__ = [
    "DarkhuntSpanExporter",
    "TelemetryEvent",
    "TelemetryEventHook",
    "ExporterStats",
]
