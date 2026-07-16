"""Client-side data masking — port of ``src/masking/sanitizer.ts``.

Compiles the bundled ruleset (66 rules, mirrored from the TS SDK's
``rules.json``) once and applies it to any string or structured value before it
leaves the process.
"""

from __future__ import annotations

import json
import os
import re
import warnings
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Pattern, Sequence

from .validators import VALIDATORS, Validator

# A compiled replacement callable: (match) -> replacement string.
_Repl = Callable[["re.Match[str]"], str]


@dataclass
class CustomPattern:
    """Operator-defined extra masking rule, merged on top of the bundled
    defaults. Register via ``DarkhuntTelemetry(mask=MaskingOptions(custom_patterns=[...]))``.

    - ``regex``: regex source (compiled, optionally case-insensitive).
    - ``marker``: replacement marker, e.g. ``[INTERNAL_ID]``.
    - ``case_sensitive``: when False, compiled case-insensitively. Defaults True.
    - ``name``: optional; surfaced in errors, not part of matching.
    """

    regex: str
    marker: str
    case_sensitive: bool = True
    name: Optional[str] = None


@dataclass
class _CompiledRule:
    marker: str
    pattern: Pattern[str]
    repl: _Repl
    validator: Optional[Validator] = None


# Zero-width characters (ZWSP, ZWNJ, ZWJ, BOM) that an attacker or careless
# serializer can splice between the bytes of a secret to defeat the masking
# regex. Strip them before the rule loop.
_ZERO_WIDTH_CHARS = re.compile("[\u200b\u200c\u200d\ufeff]")

# Reject the well-known catastrophic-backtracking shapes — a greedy class
# (., \w, \d, \s, \S) with + or * inside a group that itself has + or *.
_GREEDY_CLASS_QUANTIFIED = re.compile(r"\((?:\.|\\[wdsSW])[+*]\)[+*]")
_OVERLAPPING_ALTERNATION = re.compile(r"\((\w)\|\1\)[+*]")


def _assert_not_pathological(regex: str, name: Optional[str]) -> None:
    label = f' "{name}"' if name else ""
    if _GREEDY_CLASS_QUANTIFIED.search(regex):
        raise ValueError(
            f"[darkhunt-telemetry] Custom masking pattern{label} contains a "
            f"nested-quantifier shape that can cause catastrophic backtracking "
            f"on adversarial inputs (regex: {regex}). Rewrite without nested "
            f"quantifiers, or use possessive/atomic groups."
        )
    if _OVERLAPPING_ALTERNATION.search(regex):
        raise ValueError(
            f"[darkhunt-telemetry] Custom masking pattern{label} contains "
            f"overlapping alternation that can cause catastrophic backtracking "
            f"(regex: {regex})."
        )


def _load_defaults() -> dict:
    """Load the bundled ``rules.json`` shipped next to this module.

    Anchor on the *real* package ``darkhunt_telemetry.masking`` (which has an
    ``__init__.py``) and descend into ``rules/rules.json`` — never import the
    ``rules`` directory as a package. That directory has no ``__init__.py``, so
    treating it as a namespace subpackage breaks under some editable-install
    finders (``importlib.resources.files`` hits a ``None`` search location and
    raises ``TypeError: expected str, bytes or os.PathLike object, not NoneType``).
    Falls back to a ``__file__``-relative read, which is correct for any
    unzipped install.
    """
    try:
        from importlib.resources import files as _resource_files

        text = (
            _resource_files("darkhunt_telemetry.masking")
            .joinpath("rules")
            .joinpath("rules.json")
            .read_text(encoding="utf-8")
        )
    except Exception:
        path = os.path.join(os.path.dirname(__file__), "rules", "rules.json")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    return json.loads(text)


def _build_repl(marker: str, validator: Optional[Validator]) -> _Repl:
    """Precompute a rule's re.sub replacement callable once, at compile time.

    A function (not a string) so backslashes / group refs in ``marker`` are never
    interpreted by ``re.sub``; validated rules redact only when the validator
    confirms the match.
    """
    if validator is None:

        def repl(m: "re.Match[str]") -> str:
            return marker
    else:

        def repl(m: "re.Match[str]") -> str:
            return marker if validator(m.group(0)) else m.group(0)

    return repl


def _compile_rules(
    rules: Sequence[dict], custom_patterns: Sequence[CustomPattern]
) -> List[_CompiledRule]:
    compiled: List[_CompiledRule] = []

    for rule in rules:
        # The bundled rules are ported verbatim from the TS/ECMAScript SDK,
        # where \d \w \s are ASCII-only. Python's re treats them as UNICODE by
        # default on str patterns, which would make the SAME ruleset match
        # differently here than in the reference SDK. Compile with re.ASCII so
        # \d \w \s keep ECMA semantics — exact parity with the reference SDK.
        flags: int = re.ASCII
        if rule.get("caseSensitive") is False:
            flags |= re.IGNORECASE
        validator: Optional[Validator] = None
        validation = rule.get("validation")
        if validation:
            validator = VALIDATORS.get(validation)
            if validator is None:
                # Fail-closed: unknown validators mean we can't enforce the rule
                # safely (regex alone over-matches). Drop and warn.
                warnings.warn(
                    f'[darkhunt-telemetry] Skipping masking rule "{rule.get("name")}": '
                    f'validator "{validation}" is not implemented in this SDK version. '
                    f"Upgrade darkhunt-telemetry to enforce this rule.",
                    stacklevel=2,
                )
                continue
        marker = rule["marker"]
        compiled.append(
            _CompiledRule(
                marker=marker,
                pattern=re.compile(rule["pattern"], flags),
                repl=_build_repl(marker, validator),
                validator=validator,
            )
        )

    for cp in custom_patterns:
        _assert_not_pathological(cp.regex, cp.name)
        cp_flags = 0 if cp.case_sensitive else re.IGNORECASE
        compiled.append(
            _CompiledRule(
                marker=cp.marker,
                pattern=re.compile(cp.regex, cp_flags),
                repl=_build_repl(cp.marker, None),
            )
        )

    return compiled


class Sanitizer:
    """Compiled, ordered list of masking rules with a fast ``sanitize`` method.

    Construct once per process (typically by ``DarkhuntTelemetry``) and share
    across traces — pattern compilation runs in the constructor and the
    resulting object is read-only and concurrency-safe.
    """

    def __init__(
        self,
        rules_file: Optional[dict] = None,
        custom_patterns: Sequence[CustomPattern] = (),
    ) -> None:
        file = rules_file if rules_file is not None else _load_defaults()
        # Ruleset version stamped into the bundled JSON — useful for support.
        self.ruleset_version: str = file["version"]
        self._rules = _compile_rules(file["rules"], custom_patterns)

    def sanitize(self, input: str) -> str:
        """Apply every rule in declared order; return the redacted string."""
        if len(input) == 0:
            return input
        # Strip zero-width chars first so a spliced ZWS can't bypass the rules.
        result = _ZERO_WIDTH_CHARS.sub("", input)
        for rule in self._rules:
            result = rule.pattern.sub(rule.repl, result)
        return result

    def sanitize_unknown(self, value: Any) -> Any:
        """Recursively sanitize the string leaves of any structured value.

        Strings, numbers (stringified, run through rules, kept as the original
        number when no pattern matches), and dict KEYS are sanitized. Booleans,
        None, and other types pass through untouched. Cycle-safe: already-visited
        containers are returned as the placeholder ``"[circular]"``.
        """
        return self._walk(value, set())

    def _walk(self, value: Any, seen: set) -> Any:
        if isinstance(value, str):
            return self.sanitize(value)
        # bool is a subclass of int — pass it through untouched (matches TS).
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            s = str(value)
            # Only stringified digit-ranges plausible for SSN/phone/CC/account
            # numbers can match a rule; everything else bypasses the rule loop.
            if len(s) < 7 or len(s) > 19:
                return value
            masked = self.sanitize(s)
            return value if masked == s else masked
        if isinstance(value, (list, tuple)):
            if id(value) in seen:
                return "[circular]"
            seen.add(id(value))
            return [self._walk(v, seen) for v in value]
        if isinstance(value, dict):
            if id(value) in seen:
                return "[circular]"
            seen.add(id(value))
            out: dict = {}
            for k, v in value.items():
                # Sanitize string keys too: a secret used as a key would
                # otherwise reach the wire verbatim.
                key = self.sanitize(k) if isinstance(k, str) else k
                out[key] = self._walk(v, seen)
            return out
        return value


def safe_json_dumps(value: Any) -> str:
    """json.dumps wrapper that returns a placeholder rather than raising on
    circular refs or other unserializable values — the caller is a span-attribute
    setter on a hot path and must not fail."""
    try:
        return json.dumps(value, default=_json_default, ensure_ascii=False)
    except (TypeError, ValueError) as err:
        warnings.warn(f"darkhunt-telemetry: failed to JSON-encode value: {err}", stacklevel=2)
        return f"[unserializable: {err}]"


def _json_default(value: Any) -> str:
    return str(value)


__all__ = ["Sanitizer", "CustomPattern", "safe_json_dumps"]
