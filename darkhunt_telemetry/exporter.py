"""Darkhunt OTLP span exporter — port of ``src/exporter.ts``.

Groups spans by their (tenant, workspace, application, assessmentRun) routing
tuple, serializes each group to OTLP/protobuf, and POSTs it to that tenant's
scoped ingest endpoint with bounded retry + jittered backoff.
"""

from __future__ import annotations

import secrets
import time
import warnings
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

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


@dataclass(frozen=True)
class _RouteKey:
    tenant_id: str
    workspace_id: str
    application_id: str
    assessment_run_id: str


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
    ) -> None:
        self._base_url = _strip_trailing_slashes(base_url)
        self._api_key = api_key
        self._timeout_s = timeout_ms / 1000.0
        self._internal = internal
        self._shutdown_called = False
        self._session = session or requests.Session()
        # Dedupe drop warnings — log once per (missing-fields, span-name) pair.
        self._dropped_warned: set = set()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self._shutdown_called:
            return SpanExportResult.FAILURE
        try:
            return self._export(spans)
        except Exception:  # pragma: no cover - defensive; never raise from export
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        self._shutdown_called = True
        try:
            self._session.close()
        except Exception:  # pragma: no cover
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

    def _group_by_route(
        self, spans: Sequence[ReadableSpan]
    ) -> Dict[str, tuple]:
        groups: Dict[str, tuple] = {}
        for span in spans:
            route = self._extract_route(span)
            if route is None:
                continue
            key = f"{route.tenant_id}|{route.workspace_id}|{route.application_id}|{route.assessment_run_id}"
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
        self._warn_dropped_span(span.name, missing)
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
        return self._send_with_retry(self._build_url(route), body, route)

    def _build_url(self, route: _RouteKey) -> str:
        path_prefix = "internal" if self._internal else "otlp"
        return f"{self._base_url}/{path_prefix}/t/{_url_quote(route.tenant_id)}/v1/traces"

    def _send_with_retry(self, url: str, body: bytes, route: _RouteKey) -> bool:
        headers = {
            "Content-Type": "application/x-protobuf",
            "X-Workspace-Id": route.workspace_id,
            "X-Application-Id": route.application_id,
        }
        # Internal endpoint is permitAll; skip the bearer so we don't attach a
        # stale/empty token to in-cluster requests.
        if not self._internal:
            headers["Authorization"] = f"Bearer {self._api_key}"

        backoff = _INITIAL_BACKOFF_MS
        for attempt in range(_MAX_RETRIES):
            try:
                resp = self._session.post(
                    url, headers=headers, data=body, timeout=self._timeout_s
                )
                if resp.ok:
                    return True
                if resp.status_code not in _RETRYABLE_STATUS:
                    return False
            except requests.RequestException:
                # network/timeout — retry
                pass
            if attempt == _MAX_RETRIES - 1:
                break
            # Add 0–50% jitter so concurrent retrying clients don't synchronize.
            # secrets (not random) to satisfy strict analyzers — jitter has no
            # security impact.
            jitter_ms = secrets.randbelow(max(1, backoff // 2))
            time.sleep((backoff + jitter_ms) / 1000.0)
            backoff = min(backoff * _BACKOFF_MULTIPLIER, _MAX_BACKOFF_MS)
        return False


def _string_attr(value: object) -> str:
    return value if isinstance(value, str) else ""


def _strip_trailing_slashes(url: str) -> str:
    return url.rstrip("/")


def _url_quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


__all__ = ["DarkhuntSpanExporter"]
