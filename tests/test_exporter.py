"""Exporter: route grouping, URL building, retry/backoff, dropped-span drops."""

from __future__ import annotations

from unittest import mock

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export import SpanExportResult

from darkhunt_telemetry.attributes import ATTR
from darkhunt_telemetry.exporter import DarkhuntSpanExporter
from darkhunt_telemetry.trace import Trace


class _Resp:
    def __init__(self, status_code, ok=None):
        self.status_code = status_code
        self.ok = ok if ok is not None else (200 <= status_code < 300)


def _make_span(tracer, **routing):
    t = Trace(tracer, name="s", sanitizer=None, **routing)
    t.end()


def _spans_for(**routing):
    """Produce one finished ReadableSpan with the given routing attributes."""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    _make_span(provider.get_tracer("t"), **routing)
    spans = list(exp.get_finished_spans())
    provider.shutdown()
    return spans


def test_public_url_and_headers():
    exp = DarkhuntSpanExporter("https://api.x/trace-hub/", "dh-key", 5000, internal=False)
    spans = _spans_for(tenant_id="ten", workspace_id="wsp", application_id="app")
    with mock.patch.object(exp._session, "post", return_value=_Resp(200)) as post:
        assert exp.export(spans) == SpanExportResult.SUCCESS
    url = post.call_args.args[0]
    headers = post.call_args.kwargs["headers"]
    assert url == "https://api.x/trace-hub/otlp/t/ten/v1/traces"
    assert headers["Authorization"] == "Bearer dh-key"
    assert headers["X-Workspace-Id"] == "wsp"
    assert headers["X-Application-Id"] == "app"


def test_internal_url_omits_auth():
    exp = DarkhuntSpanExporter("https://api.x/trace-hub", "", 5000, internal=True)
    spans = _spans_for(tenant_id="ten", workspace_id="wsp", application_id="app")
    with mock.patch.object(exp._session, "post", return_value=_Resp(200)) as post:
        exp.export(spans)
    url = post.call_args.args[0]
    headers = post.call_args.kwargs["headers"]
    assert url == "https://api.x/trace-hub/internal/t/ten/v1/traces"
    assert "Authorization" not in headers


def test_drops_span_without_routing():
    exp = DarkhuntSpanExporter("https://api.x/trace-hub", "k", 5000)
    spans = _spans_for(tenant_id="ten", workspace_id="", application_id="app")
    with mock.patch.object(exp._session, "post") as post:
        # No route -> nothing posted, still SUCCESS (dropped, not failed).
        assert exp.export(spans) == SpanExportResult.SUCCESS
        post.assert_not_called()


def test_non_retryable_status_fails_fast():
    exp = DarkhuntSpanExporter("https://api.x/trace-hub", "k", 5000)
    spans = _spans_for(tenant_id="ten", workspace_id="wsp", application_id="app")
    with mock.patch.object(exp._session, "post", return_value=_Resp(401)) as post:
        assert exp.export(spans) == SpanExportResult.FAILURE
        assert post.call_count == 1  # 401 is not retried


def test_retryable_status_retries(monkeypatch):
    exp = DarkhuntSpanExporter("https://api.x/trace-hub", "k", 5000)
    spans = _spans_for(tenant_id="ten", workspace_id="wsp", application_id="app")
    monkeypatch.setattr("darkhunt_telemetry.exporter.time.sleep", lambda *_: None)
    with mock.patch.object(exp._session, "post", return_value=_Resp(503)) as post:
        assert exp.export(spans) == SpanExportResult.FAILURE
        assert post.call_count == 3  # MAX_RETRIES


def test_route_grouping_splits_by_tenant():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exp_mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp_mem))
    tr = provider.get_tracer("t")
    _make_span(tr, tenant_id="A", workspace_id="w", application_id="a")
    _make_span(tr, tenant_id="B", workspace_id="w", application_id="a")
    spans = list(exp_mem.get_finished_spans())
    provider.shutdown()

    exp = DarkhuntSpanExporter("https://api.x/trace-hub", "k", 5000)
    with mock.patch.object(exp._session, "post", return_value=_Resp(200)) as post:
        exp.export(spans)
    # Two tenants -> two POSTs to two tenant-scoped URLs.
    urls = {c.args[0] for c in post.call_args_list}
    assert urls == {
        "https://api.x/trace-hub/otlp/t/A/v1/traces",
        "https://api.x/trace-hub/otlp/t/B/v1/traces",
    }
