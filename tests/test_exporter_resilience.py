"""Delivery-resilience tests for the exporter/client.

Covers the two hardening changes:
  A) delivery observability — the ``on_error`` hook + counters (``stats()``),
     and that hook exceptions never break ``export()``.
  B) bounded, interruptible retry backoff — network-error retries, cumulative
     backoff cap, shutdown-aborts-mid-retry, and per-group failure isolation.

Unlike ``test_exporter.py``, this suite decodes the POSTed OTLP protobuf body
and asserts on its contents.
"""

from __future__ import annotations

from typing import List, Optional
from unittest import mock

import requests
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from darkhunt_telemetry.attributes import ATTR
from darkhunt_telemetry.client import DarkhuntTelemetry
from darkhunt_telemetry.exporter import (
    DarkhuntSpanExporter,
    ExporterStats,
    TelemetryEvent,
)
from darkhunt_telemetry.trace import Trace


class _Resp:
    def __init__(self, status_code: int, ok: Optional[bool] = None) -> None:
        self.status_code = status_code
        self.ok = ok if ok is not None else (200 <= status_code < 300)


def _make_span(tracer, **routing) -> None:
    t = Trace(tracer, name="s", sanitizer=None, **routing)
    t.end()


def _spans_for(**routing):
    """Produce finished ReadableSpan(s) with the given routing attributes."""
    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    _make_span(provider.get_tracer("t"), **routing)
    spans = list(exp.get_finished_spans())
    provider.shutdown()
    return spans


def _decode(body: bytes):
    """Decode a POSTed OTLP/protobuf body into (span_names, attr_dict_by_name)."""
    req = ExportTraceServiceRequest()
    req.ParseFromString(body)
    names: List[str] = []
    attrs_by_name = {}
    for rs in req.resource_spans:
        for ss in rs.scope_spans:
            for span in ss.spans:
                names.append(span.name)
                attrs = {}
                for kv in span.attributes:
                    attrs[kv.key] = kv.value.string_value
                attrs_by_name[span.name] = attrs
    return names, attrs_by_name


def _no_sleep(monkeypatch) -> None:
    monkeypatch.setattr("darkhunt_telemetry.exporter.time.sleep", lambda *_: None)


# --------------------------------------------------------------------------- #
# Serialized-body assertions (the existing suite never decodes the body).
# --------------------------------------------------------------------------- #


def test_posted_body_decodes_to_expected_span():
    exp = DarkhuntSpanExporter("https://api.x/trace-hub", "dh-key", 5000)
    spans = _spans_for(tenant_id="ten", workspace_id="wsp", application_id="app")
    with mock.patch.object(exp._session, "post", return_value=_Resp(200)) as post:
        assert exp.export(spans) == SpanExportResult.SUCCESS

    body = post.call_args.kwargs["data"]
    assert isinstance(body, (bytes, bytearray)) and len(body) > 0
    names, attrs = _decode(body)
    assert names == ["s"]
    assert attrs["s"][ATTR.TENANT_ID] == "ten"
    assert attrs["s"][ATTR.WORKSPACE_ID] == "wsp"
    assert attrs["s"][ATTR.APPLICATION_ID] == "app"


# --------------------------------------------------------------------------- #
# Retry paths + counters.
# --------------------------------------------------------------------------- #


def test_network_error_retry_path_fires_hook(monkeypatch):
    _no_sleep(monkeypatch)
    events: List[TelemetryEvent] = []
    exp = DarkhuntSpanExporter("https://api.x/trace-hub", "k", 5000, on_error=events.append)
    spans = _spans_for(tenant_id="ten", workspace_id="wsp", application_id="app")
    with mock.patch.object(
        exp._session, "post", side_effect=requests.ConnectionError("boom")
    ) as post:
        assert exp.export(spans) == SpanExportResult.FAILURE
        assert post.call_count == 3  # all retries exhausted on a network error

    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "export_failed"
    assert ev.http_status is None  # network error: never got a status
    assert "ConnectionError" in (ev.error or "")
    assert ev.tenant_id == "ten"
    assert exp.stats() == ExporterStats(
        spans_exported=0, spans_dropped_unroutable=0, export_failures=1
    )


def test_hook_fires_on_export_failure_with_status(monkeypatch):
    _no_sleep(monkeypatch)
    events: List[TelemetryEvent] = []
    exp = DarkhuntSpanExporter("https://api.x/trace-hub", "k", 5000, on_error=events.append)
    spans = _spans_for(tenant_id="ten", workspace_id="wsp", application_id="app")
    with mock.patch.object(exp._session, "post", return_value=_Resp(503)):
        assert exp.export(spans) == SpanExportResult.FAILURE

    assert len(events) == 1
    assert events[0].kind == "export_failed"
    assert events[0].http_status == 503
    assert events[0].span_count == 1
    assert events[0].group_count == 1
    assert exp.stats().export_failures == 1


def test_hook_fires_on_dropped_unroutable_span():
    events: List[TelemetryEvent] = []
    exp = DarkhuntSpanExporter("https://api.x/trace-hub", "k", 5000, on_error=events.append)
    spans = _spans_for(tenant_id="ten", workspace_id="", application_id="app")
    with mock.patch.object(exp._session, "post") as post:
        assert exp.export(spans) == SpanExportResult.SUCCESS
        post.assert_not_called()

    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "spans_dropped"
    assert ev.span_count == 1
    assert ev.missing_attributes == ("workspaceId",)
    assert exp.stats() == ExporterStats(
        spans_exported=0, spans_dropped_unroutable=1, export_failures=0
    )


def test_successful_export_increments_exported_counter():
    exp = DarkhuntSpanExporter("https://api.x/trace-hub", "k", 5000)
    spans = _spans_for(tenant_id="ten", workspace_id="wsp", application_id="app")
    with mock.patch.object(exp._session, "post", return_value=_Resp(200)):
        assert exp.export(spans) == SpanExportResult.SUCCESS
    assert exp.stats().spans_exported == 1


def test_throwing_hook_never_breaks_export(monkeypatch):
    _no_sleep(monkeypatch)

    def _boom(_event: TelemetryEvent) -> None:
        raise RuntimeError("hook exploded")

    exp = DarkhuntSpanExporter("https://api.x/trace-hub", "k", 5000, on_error=_boom)
    spans = _spans_for(tenant_id="ten", workspace_id="wsp", application_id="app")
    with mock.patch.object(exp._session, "post", return_value=_Resp(503)):
        # Hook throws, but export() must still return a normal result.
        assert exp.export(spans) == SpanExportResult.FAILURE
    assert exp.stats().export_failures == 1


# --------------------------------------------------------------------------- #
# Bounded / interruptible backoff.
# --------------------------------------------------------------------------- #


def test_backoff_is_bounded_by_timeout_budget(monkeypatch):
    slept: List[float] = []
    monkeypatch.setattr("darkhunt_telemetry.exporter.time.sleep", lambda s: slept.append(s))
    # timeout_ms=100 -> a 0.1s cumulative backoff budget. The natural first
    # backoff (~1s) must be clipped so total sleep never exceeds the budget.
    exp = DarkhuntSpanExporter("https://api.x/trace-hub", "k", timeout_ms=100)
    spans = _spans_for(tenant_id="ten", workspace_id="wsp", application_id="app")
    with mock.patch.object(exp._session, "post", return_value=_Resp(503)):
        assert exp.export(spans) == SpanExportResult.FAILURE

    total = sum(slept)
    assert total <= 0.1 + 1e-6, f"cumulative backoff {total}s exceeded 0.1s budget"


def test_shutdown_during_retry_aborts_promptly():
    exp = DarkhuntSpanExporter("https://api.x/trace-hub", "k", 5000)
    spans = _spans_for(tenant_id="ten", workspace_id="wsp", application_id="app")

    # First backoff sleep triggers shutdown; the sliced wait must then notice the
    # shutdown event and stop retrying instead of sleeping out the full backoff.
    def _sleep_then_shutdown(_seconds: float) -> None:
        exp.shutdown()

    with mock.patch("darkhunt_telemetry.exporter.time.sleep", _sleep_then_shutdown):
        with mock.patch.object(exp._session, "post", return_value=_Resp(503)) as post:
            assert exp.export(spans) == SpanExportResult.FAILURE

    # Only the first attempt ran; the retry was aborted by shutdown.
    assert post.call_count == 1


def test_export_after_shutdown_returns_failure():
    exp = DarkhuntSpanExporter("https://api.x/trace-hub", "k", 5000)
    exp.shutdown()
    spans = _spans_for(tenant_id="ten", workspace_id="wsp", application_id="app")
    with mock.patch.object(exp._session, "post") as post:
        assert exp.export(spans) == SpanExportResult.FAILURE
        post.assert_not_called()


def test_failing_group_does_not_block_other_groups(monkeypatch):
    _no_sleep(monkeypatch)
    events: List[TelemetryEvent] = []

    exp_mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp_mem))
    tr = provider.get_tracer("t")
    _make_span(tr, tenant_id="A", workspace_id="w", application_id="a")
    _make_span(tr, tenant_id="B", workspace_id="w", application_id="a")
    spans = list(exp_mem.get_finished_spans())
    provider.shutdown()

    exp = DarkhuntSpanExporter("https://api.x/trace-hub", "k", 5000, on_error=events.append)

    def _by_tenant(url, **_kwargs):
        # Tenant A always 503s; tenant B succeeds. A failing group must not stop
        # B from being exported.
        return _Resp(503) if "/t/A/" in url else _Resp(200)

    with mock.patch.object(exp._session, "post", side_effect=_by_tenant) as post:
        assert exp.export(spans) == SpanExportResult.FAILURE

    posted_urls = {c.args[0] for c in post.call_args_list}
    assert "https://api.x/trace-hub/otlp/t/A/v1/traces" in posted_urls
    assert "https://api.x/trace-hub/otlp/t/B/v1/traces" in posted_urls
    # B still delivered despite A failing.
    assert exp.stats().spans_exported == 1
    assert exp.stats().export_failures == 1
    failed = [e for e in events if e.kind == "export_failed"]
    assert len(failed) == 1 and failed[0].tenant_id == "A"


# --------------------------------------------------------------------------- #
# Client surface.
# --------------------------------------------------------------------------- #


def test_flush_returns_bool_true_when_enabled():
    dh = DarkhuntTelemetry(
        internal=True,
        tenant_id="t",
        workspace_id="w",
        application_id="a",
    )
    try:
        result = dh.flush()
        assert isinstance(result, bool)
        assert result is True  # nothing buffered -> flush succeeds
    finally:
        dh.shutdown()


def test_flush_returns_true_when_disabled():
    dh = DarkhuntTelemetry(enabled=False)
    assert dh.flush() is True


def test_client_wires_hook_and_surfaces_stats():
    events: List[TelemetryEvent] = []

    def hook(event: TelemetryEvent) -> None:
        events.append(event)

    dh = DarkhuntTelemetry(
        internal=True,
        tenant_id="t",
        workspace_id="w",
        application_id="a",
        on_error=hook,
    )
    try:
        stats = dh.stats()
        assert isinstance(stats, ExporterStats)
        assert stats == ExporterStats(
            spans_exported=0, spans_dropped_unroutable=0, export_failures=0
        )
        # The hook reached the underlying exporter.
        assert dh._exporter is not None
        assert dh._exporter._on_error is hook
    finally:
        dh.shutdown()


def test_client_stats_none_when_disabled():
    dh = DarkhuntTelemetry(enabled=False)
    assert dh.stats() is None
