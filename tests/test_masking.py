"""Masking layer: validators, sanitizer recursion, custom patterns, ruleset."""

from __future__ import annotations

import json
from importlib.resources import files

import pytest

from darkhunt_telemetry.masking import CustomPattern, Sanitizer
from darkhunt_telemetry.masking.validators import (
    aba,
    base58check,
    bech32,
    credit_card,
    eip55,
    iban_mod97,
    luhn,
)


def _rules():
    # Anchor on the real package (never the __init__-less ``rules`` namespace
    # subdir, which breaks importlib.resources on some Pythons — see
    # sanitizer._load_defaults).
    text = (
        files("darkhunt_telemetry.masking")
        .joinpath("rules")
        .joinpath("rules.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(text)


def test_all_bundled_examples_are_masked():
    s = Sanitizer()
    for rule in _rules()["rules"]:
        for ex in rule.get("examples", []):
            assert s.sanitize(ex) != ex, f"{rule['name']} did not mask {ex!r}"


def test_luhn_and_credit_card():
    assert luhn("4242424242424242")
    assert not luhn("4242424242424241")
    assert credit_card("4242 4242 4242 4242")  # Visa
    assert not credit_card("1234567890123456")  # no valid IIN


def test_aba_iban_validators():
    assert aba("021000021")  # a real ABA routing number
    assert not aba("021000022")
    assert iban_mod97("GB82WEST12345698765432")
    assert not iban_mod97("GB82WEST12345698765433")


def test_crypto_validators():
    # Genesis Bitcoin address (Base58Check).
    assert base58check("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")
    assert not base58check("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb")
    # A valid bech32 segwit address.
    assert bech32("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4")
    # EIP-55 mixed-case checksummed Ethereum address.
    assert eip55("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed")
    assert not eip55("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAeD")


def test_sanitize_recurses_structures():
    s = Sanitizer()
    value = {
        "user": "alice@example.com",
        "nested": ["bob@example.com", 123, True, None],
    }
    out = s.sanitize_unknown(value)
    assert out["user"] == "[EMAIL]"
    assert out["nested"][0] == "[EMAIL]"
    assert out["nested"][1] == 123  # small number untouched
    assert out["nested"][2] is True  # bool passes through
    assert out["nested"][3] is None


def test_sanitize_masks_dict_keys():
    s = Sanitizer()
    out = s.sanitize_unknown({"alice@example.com": 1})
    assert "[EMAIL]" in out


def test_sanitize_cycle_safe():
    s = Sanitizer()
    a = {}
    a["self"] = a
    out = s.sanitize_unknown(a)
    assert out["self"] == "[circular]"


def test_zero_width_bypass_is_stripped():
    s = Sanitizer()
    # A zero-width space spliced into an email must not defeat masking.
    assert s.sanitize("alice@ex​ample.com") == "[EMAIL]"


def test_custom_pattern_merged_after_defaults():
    s = Sanitizer(custom_patterns=[CustomPattern(regex=r"TICKET-\d+", marker="[TICKET]")])
    assert s.sanitize("see TICKET-123") == "see [TICKET]"


def test_pathological_custom_pattern_rejected():
    # Build the pattern outside the raises block so only the Sanitizer call
    # (the one expected to throw) is under assertion.
    bad = [CustomPattern(regex=r"(\w+)+", marker="[X]")]
    with pytest.raises(ValueError):
        Sanitizer(custom_patterns=bad)


def test_ruleset_version_exposed():
    assert Sanitizer().ruleset_version == _rules()["version"]
