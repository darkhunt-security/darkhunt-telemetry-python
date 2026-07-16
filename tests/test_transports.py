"""Transport helpers: HTTP traceparent header + queue message metadata."""

from __future__ import annotations

from darkhunt_telemetry.transports import (
    HANDOFF_MESSAGE_META_KEY,
    TRACEPARENT_HEADER,
    handoff_from_http_headers,
    handoff_from_message_meta,
    handoff_to_http_headers,
    handoff_to_message_meta,
    handoffs_from_messages,
)

TOKEN = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"


def test_http_round_trip_preserves_existing_headers():
    headers = handoff_to_http_headers(TOKEN, {"content-type": "application/json"})
    assert headers[TRACEPARENT_HEADER] == TOKEN
    assert headers["content-type"] == "application/json"
    assert handoff_from_http_headers(headers) == TOKEN


def test_http_case_insensitive_and_repeated():
    assert handoff_from_http_headers({"TraceParent": TOKEN}) == TOKEN
    assert handoff_from_http_headers({"traceparent": [TOKEN, "other"]}) == TOKEN
    assert handoff_from_http_headers({}) is None


def test_http_headers_object_with_get():
    class Headers:
        def __init__(self, d):
            self._d = {k.lower(): v for k, v in d.items()}

        def get(self, name):
            return self._d.get(name.lower())

    assert handoff_from_http_headers(Headers({"Traceparent": TOKEN})) == TOKEN


def test_queue_round_trip():
    meta = handoff_to_message_meta(TOKEN, {"content-type": "app/json"})
    assert meta[HANDOFF_MESSAGE_META_KEY] == TOKEN
    assert handoff_from_message_meta(meta) == TOKEN


def test_queue_coerces_bytes_and_sqs_wrapper():
    assert handoff_from_message_meta({HANDOFF_MESSAGE_META_KEY: TOKEN.encode()}) == TOKEN
    assert handoff_from_message_meta({HANDOFF_MESSAGE_META_KEY: {"StringValue": TOKEN}}) == TOKEN


def test_queue_fan_in_dedupes_and_orders():
    m1 = {HANDOFF_MESSAGE_META_KEY: "a"}
    m2 = {HANDOFF_MESSAGE_META_KEY: "b"}
    m3 = {HANDOFF_MESSAGE_META_KEY: "a"}  # duplicate
    m4 = {}  # no handoff
    assert handoffs_from_messages([m1, m2, m3, m4]) == ["a", "b"]
