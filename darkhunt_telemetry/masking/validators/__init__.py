"""Post-match validators for the masking ruleset.

A validator returns True to confirm a regex match should be redacted. The
``VALIDATORS`` mapping's keys MUST stay aligned with the schema's ``validation``
enum (``@darkhunt-security/masking-schema``'s ``MaskingRule.validation``).
"""

from __future__ import annotations

from typing import Callable, Dict

from .aba import aba
from .base58check import base58check
from .bech32 import bech32
from .credit_card import credit_card
from .eip55 import eip55
from .iban_mod97 import iban_mod97
from .luhn import luhn

Validator = Callable[[str], bool]

VALIDATORS: Dict[str, Validator] = {
    "aba": aba,
    "base58check": base58check,
    "bech32": bech32,
    "credit_card": credit_card,
    "eip55": eip55,
    "iban_mod97": iban_mod97,
    "luhn": luhn,
}

__all__ = [
    "Validator",
    "VALIDATORS",
    "aba",
    "base58check",
    "bech32",
    "credit_card",
    "eip55",
    "iban_mod97",
    "luhn",
]
