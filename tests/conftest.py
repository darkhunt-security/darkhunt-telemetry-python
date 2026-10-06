"""Shared test fixtures — an in-memory OTel tracer to inspect emitted spans."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from darkhunt_telemetry.guard import reset_config


class _Mem:
    def __init__(self):
        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.tracer = self.provider.get_tracer("test")

    def spans(self):
        return list(self.exporter.get_finished_spans())

    def by_name(self, name):
        return [s for s in self.spans() if s.name == name]


@pytest.fixture
def mem():
    m = _Mem()
    yield m
    m.provider.shutdown()


class _Verify:
    """A /verify stand-in: records each request and answers from ``rules``,
    a ``{(stage, tool): response}`` map (ALLOW when absent)."""

    def __init__(self):
        self.requests: list = []
        self.rules: dict = {}
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": body}
                )
                answer = stub.rules.get(
                    (body["stage"], body["tool"]["name"]), {"decision": "ALLOW"}
                )
                payload = json.dumps({"stage": body["stage"], "failed": False, **answer}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def deny(self, stage, tool, rule="No sending outside the care team"):
        self.rules[(stage, tool)] = {
            "decision": "DENY",
            "matchedRules": [{"ruleId": "r-1", "ruleName": rule, "action": "DENY"}],
        }

    def stages(self):
        return [(r["body"]["stage"], r["body"]["tool"]["name"]) for r in self.requests]


@pytest.fixture
def verify():
    v = _Verify()
    yield v
    v.server.shutdown()
    reset_config()
