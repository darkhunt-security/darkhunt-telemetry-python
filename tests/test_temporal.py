"""Temporal handoff helpers (unit-level, no live worker)."""

from __future__ import annotations

import pytest

pytest.importorskip("temporalio")

from darkhunt_telemetry.temporal import HANDOFF_META, child_args, current_handoff
from darkhunt_telemetry.temporal.interceptors import _decode_header


def test_child_args_no_override_returns_input_unchanged():
    inp = {"task": "x"}
    assert child_args(inp) is inp
    assert child_args(inp, []) is inp


def test_child_args_attaches_hidden_meta():
    out = child_args({"task": "x"}, ["tok"])
    assert out["task"] == "x"
    assert out[HANDOFF_META] == ["tok"]
    # original not mutated
    assert "task" in out


def test_child_args_rejects_non_mapping():
    with pytest.raises(TypeError):
        child_args("not-a-dict", ["tok"])


def test_current_handoff_default_none():
    assert current_handoff() is None


def test_decode_header_round_trip():
    import temporalio.converter

    pc = temporalio.converter.default().payload_converter
    payload = pc.to_payload(["tok-a", "tok-b"])
    from darkhunt_telemetry.temporal.handoff_header import HANDOFF_HEADER

    assert _decode_header({HANDOFF_HEADER: payload}, pc) == ["tok-a", "tok-b"]
    assert _decode_header({}, pc) is None
    assert _decode_header(None, pc) is None
