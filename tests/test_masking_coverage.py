"""Coverage-hardening for the masking layer — the client-side security redaction.

Targets the fail-closed, edge, and validator branches that the existing suite
leaves uncovered: unknown-validator skip, the __file__ fallback in
``_load_defaults``, ``safe_json_dumps`` on unserializable input, the numeric /
cycle branches of ``sanitize_unknown``, the ReDoS guard, and the negative /
IIN-range branches of every crypto validator.
"""

from __future__ import annotations

import pytest

import darkhunt_telemetry.masking.sanitizer as san
from darkhunt_telemetry.masking import CustomPattern, Sanitizer, safe_json_dumps
from darkhunt_telemetry.masking.validators.aba import aba
from darkhunt_telemetry.masking.validators.base58check import base58check
from darkhunt_telemetry.masking.validators.bech32 import bech32
from darkhunt_telemetry.masking.validators.credit_card import (
    _is_diners,
    _is_discover,
    _is_mastercard,
    credit_card,
)
from darkhunt_telemetry.masking.validators.eip55 import eip55
from darkhunt_telemetry.masking.validators.iban_mod97 import iban_mod97
from darkhunt_telemetry.masking.validators.luhn import luhn

# --- Sanitizer: fail-closed + edge branches ---------------------------------


def test_unknown_validator_is_skipped_fail_closed():
    """A rule naming a validator this SDK doesn't implement is dropped with a
    warning rather than applied regex-only (which would over-match)."""
    rules_file = {
        "version": "test-1",
        "rules": [
            {
                "name": "mystery",
                "pattern": r"\d{9}",
                "marker": "[X]",
                "validation": "does_not_exist",
            }
        ],
    }
    with pytest.warns(UserWarning, match="not implemented"):
        s = Sanitizer(rules_file=rules_file)
    # Rule was skipped, so a 9-digit string passes through untouched.
    assert s.sanitize("123456789") == "123456789"


def test_load_defaults_falls_back_to_file_read(monkeypatch):
    """When importlib.resources can't resolve the bundled rules, the
    __file__-relative read still loads a valid ruleset."""

    def boom(*_a, **_k):
        raise RuntimeError("no resources finder")

    monkeypatch.setattr("importlib.resources.files", boom)
    data = san._load_defaults()
    assert "version" in data and isinstance(data["rules"], list) and data["rules"]


def test_sanitize_empty_string_is_noop():
    assert Sanitizer().sanitize("") == ""


def test_redos_guard_rejects_overlapping_alternation():
    patterns = [CustomPattern(regex=r"(a|a)+", marker="[X]", name="bad")]
    with pytest.raises(ValueError, match="overlapping alternation"):
        Sanitizer(custom_patterns=patterns)


def test_redos_guard_rejects_nested_quantifier():
    patterns = [CustomPattern(regex=r"(\d+)+", marker="[X]")]
    with pytest.raises(ValueError, match="nested-quantifier|catastrophic"):
        Sanitizer(custom_patterns=patterns)


# --- safe_json_dumps ---------------------------------------------------------


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


# --- sanitize_unknown: numeric + cycle branches ------------------------------


def test_sanitize_unknown_numeric_branches():
    # A custom rule that masks any 10-digit run makes the numeric path
    # deterministic regardless of the bundled ruleset.
    s = Sanitizer(custom_patterns=[CustomPattern(regex=r"\d{10}", marker="[NUM]")])
    # 10-digit int -> stringified, masked -> returned as the masked STRING.
    assert s.sanitize_unknown(1234567890) == "[NUM]"
    # Too short (<7 digits) -> bypasses the rule loop, returned as-is int.
    assert s.sanitize_unknown(123) == 123
    # Too long (>19 digits) -> bypasses, returned as-is.
    big = 12345678901234567890
    assert s.sanitize_unknown(big) == big
    # In-range but non-matching -> original number preserved (not stringified).
    assert s.sanitize_unknown(1234567) == 1234567


def test_sanitize_unknown_marks_cycles():
    s = Sanitizer()
    lst: list = [1]
    lst.append(lst)
    assert s.sanitize_unknown(lst)[1] == "[circular]"
    d: dict = {}
    d["self"] = d
    assert s.sanitize_unknown(d)["self"] == "[circular]"


def test_sanitize_unknown_passes_through_bool_and_none():
    s = Sanitizer()
    assert s.sanitize_unknown(True) is True
    assert s.sanitize_unknown(None) is None


# --- validators: negative + IIN-range branches -------------------------------


def test_luhn_rejects_non_digit_and_empty():
    assert luhn("12a4") is False
    assert luhn("") is False


def test_aba_length_and_checksum():
    assert aba("021000021") is True  # Chase routing number
    assert aba("021000022") is False  # valid length, bad checksum
    assert aba("12345") is False  # wrong length
    assert aba("12345678X") is False  # non-digit in a 9-char string


def test_iban_length_and_charset():
    assert iban_mod97("GB82WEST12345698765432") is True
    assert iban_mod97("gb82west12345698765432") is True  # lowercase letters
    assert iban_mod97("DE12") is False  # too short
    assert iban_mod97("GB82!EST12345698765432") is False  # invalid char


def test_credit_card_end_to_end():
    assert credit_card("4111111111111111") is True  # Visa, Luhn-valid
    assert credit_card("4111111111111112") is False  # Luhn fails
    assert credit_card("1234") is False  # too short


def test_credit_card_iin_ranges():
    # Mastercard 2-series boundaries (2221-2720) and 5-series.
    assert _is_mastercard("2221000000000000", 16) is True
    assert _is_mastercard("2720000000000000", 16) is True
    assert _is_mastercard("2721000000000000", 16) is False
    assert _is_mastercard("5100000000000000", 16) is True
    assert _is_mastercard("6000000000000000", 16) is False
    assert _is_mastercard("4000000000000000", 15) is False  # wrong length
    # Discover ranges.
    assert _is_discover("6011000000000000", 16) is True
    assert _is_discover("6440000000000000", 16) is True  # 644-649
    assert _is_discover("6410000000000000", 16) is False  # 641 out of range
    assert _is_discover("6221260000000000", 16) is True  # 622126-622925
    assert _is_discover("6229260000000000", 16) is False
    assert _is_discover("6300000000000000", 16) is False
    # Diners ranges.
    assert _is_diners("30500000000000", 14) is True  # 305
    assert _is_diners("30600000000000", 14) is False  # 306 out of 300-305
    assert _is_diners("36000000000000", 14) is True  # 36
    assert _is_diners("30000000000000", 13) is False  # wrong length


def test_base58check_branches():
    assert base58check("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa") is True  # genesis addr
    assert base58check("") is False
    assert base58check("0OIl") is False  # chars outside the base58 alphabet
    assert base58check("1") is False  # decodes too short for a checksum


def test_bech32_branches():
    assert bech32("a12uel5l") is True  # BIP-173 canonical valid vector
    assert bech32("A12UEL5L") is True  # all-uppercase accepted
    assert bech32("bc1" + "q" * 90) is False  # length > 90
    assert bech32("Bc1qqqqqq") is False  # mixed case
    assert bech32("1qqqqqqq") is False  # separator at index 0
    assert bech32("bc1qqqqqqb") is False  # 'b' not in the bech32 charset


def test_eip55_case_checksum():
    valid = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
    assert eip55(valid) is True
    # Flip one letter's case -> checksum no longer matches.
    assert eip55("0x5AAeb6053F3E94C9b9A09f33669435E7Ef1BeAed") is False
    assert eip55(valid.lower()) is True  # all-lower: no case pattern to verify
    assert eip55(valid.upper().replace("0X", "0x")) is True  # all-upper
    assert eip55("0x1234") is False  # wrong shape
