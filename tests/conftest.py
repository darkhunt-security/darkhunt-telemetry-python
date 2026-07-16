"""Shared test fixtures — an in-memory OTel tracer to inspect emitted spans."""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from darkhunt_telemetry.masking import Sanitizer


class _Mem:
    def __init__(self):
        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.tracer = self.provider.get_tracer("test")
        self.sanitizer = Sanitizer()

    def spans(self):
        return list(self.exporter.get_finished_spans())

    def by_name(self, name):
        return [s for s in self.spans() if s.name == name]


@pytest.fixture
def mem():
    m = _Mem()
    yield m
    m.provider.shutdown()
