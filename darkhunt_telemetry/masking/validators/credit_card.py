"""Credit-card validator: Luhn-passing AND length matches a known IIN range.

Necessary as a separate validator from plain Luhn because many non-card
identifiers (e.g. IMEI) also Luhn-pass — the IIN range gate eliminates false
positives where a 15- or 16-digit value happens to checksum but isn't a card.
"""

from __future__ import annotations

import re

from .luhn import luhn

_STRIP = re.compile(r"[\s-]")


def credit_card(input: str) -> bool:
    digits = _STRIP.sub("", input)
    return (
        13 <= len(digits) <= 16 and _has_valid_iin(digits) and luhn(digits)
    )


def _has_valid_iin(d: str) -> bool:
    length = len(d)
    if length < 13:
        return False
    return (
        _is_visa(d, length)
        or _is_mastercard(d, length)
        or _is_amex(d, length)
        or _is_diners(d, length)
        or _is_jcb(d, length)
        or _is_discover(d, length)
    )


def _is_visa(d: str, length: int) -> bool:
    """Visa: 4XXX, length 13 or 16."""
    return d.startswith("4") and length in (13, 16)


def _is_mastercard(d: str, length: int) -> bool:
    """Mastercard: 51-55 OR 2221-2720, length 16."""
    if length != 16:
        return False
    if d.startswith("5"):
        return "1" <= d[1] <= "5"
    if d.startswith("2"):
        prefix = int(d[:4])
        return 2221 <= prefix <= 2720
    return False


def _is_amex(d: str, length: int) -> bool:
    """Amex: 34 or 37, length 15."""
    return length == 15 and d.startswith(("34", "37"))


def _is_diners(d: str, length: int) -> bool:
    """Diners: 300-305, 36, or 38, length 14."""
    if length != 14:
        return False
    if d.startswith("30"):
        return "0" <= d[2] <= "5"
    return d.startswith(("36", "38"))


def _is_jcb(d: str, length: int) -> bool:
    """JCB: 35XX, length 16."""
    return length == 16 and d.startswith("35")


def _is_discover(d: str, length: int) -> bool:
    """Discover: 6011, 65, 644-649, or 622126-622925; length 16."""
    if length != 16:
        return False
    if d.startswith("6011"):
        return True
    if d.startswith("65"):
        return True
    if d.startswith("64"):
        return "4" <= d[2] <= "9"
    if d.startswith("62"):
        prefix = int(d[:6])
        return 622126 <= prefix <= 622925
    return False
