"""ECMAScript parity for the bundled ruleset.

The bundled rules are ported verbatim from the TS/ECMAScript SDK where
``\\d \\w \\s`` are ASCII-only. Python's ``re`` treats them as UNICODE by
default, so the sanitizer compiles the bundled rules with ``re.ASCII`` to keep
exact parity. These tests lock that behavior in.
"""

from __future__ import annotations

import json
from importlib.resources import files

from darkhunt_telemetry.masking import CustomPattern, Sanitizer


def _rules():
    # Anchor on the real package (never the __init__-less ``rules`` namespace
    # subdir, which breaks importlib.resources on Py3.9 -- see
    # sanitizer._load_defaults / tests/test_masking.py).
    text = (
        files("darkhunt_telemetry.masking")
        .joinpath("rules")
        .joinpath("rules.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(text)


def test_all_bundled_examples_still_masked_under_ascii():
    # Adding re.ASCII must not regress any bundled example (all are ASCII).
    s = Sanitizer()
    for rule in _rules()["rules"]:
        for ex in rule.get("examples", []):
            assert s.sanitize(ex) != ex, f"{rule['name']} did not mask {ex!r}"


def test_ssn_ascii_masked_but_unicode_digits_not():
    s = Sanitizer()

    # ASCII SSN -- must be masked by the \d{3}-\d{2}-\d{4} rule.
    ascii_ssn = "123-45-6789"
    assert s.sanitize(ascii_ssn) == "[SSN]"

    # Same shape built from non-ASCII Unicode decimal digits. Under ECMA/ASCII
    # semantics \d does NOT match these, so nothing is redacted. If Python's
    # default UNICODE \d were in effect these WOULD be masked -- the divergence
    # this fix removes. Built with explicit \uXXXX escapes.
    #
    # "123-45-6789" spelled with Arabic-Indic digits U+0661..U+0669.
    arabic_ssn = "\u0661\u0662\u0663-\u0664\u0665-\u0666\u0667\u0668\u0669"
    assert s.sanitize(arabic_ssn) == arabic_ssn
    assert "[SSN]" not in s.sanitize(arabic_ssn)

    # Same shape spelled with fullwidth digits U+FF11..U+FF19.
    fullwidth_ssn = "\uff11\uff12\uff13-\uff14\uff15-\uff16\uff17\uff18\uff19"
    assert s.sanitize(fullwidth_ssn) == fullwidth_ssn
    assert "[SSN]" not in s.sanitize(fullwidth_ssn)


def test_custom_pattern_semantics_preserved():
    # Custom patterns keep the user's chosen semantics -- the SDK does NOT force
    # them to ASCII. A plain ASCII match works as the user intended.
    s = Sanitizer(
        custom_patterns=[CustomPattern(regex=r"\d+id", marker="[X]", case_sensitive=True)]
    )
    assert s.sanitize("42id") == "[X]"
