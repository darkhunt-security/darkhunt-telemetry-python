"""``safe_json_dumps`` — the attribute serializer must never raise."""

from __future__ import annotations

import pytest

from darkhunt_telemetry.serialization import safe_json_dumps


def test_safe_json_dumps_returns_placeholder_on_circular():
    a: list = [1]
    a.append(a)  # self-referential -> json raises even with default=str
    with pytest.warns(UserWarning, match="failed to JSON-encode"):
        out = safe_json_dumps(a)
    assert out.startswith("[unserializable")


def test_safe_json_dumps_uses_str_default_for_odd_types():
    class Weird:
        def __str__(self) -> str:
            return "weird!"

    assert "weird!" in safe_json_dumps({"k": Weird()})


def test_safe_json_dumps_keeps_non_ascii():
    assert safe_json_dumps({"k": "héllo"}) == '{"k": "héllo"}'
