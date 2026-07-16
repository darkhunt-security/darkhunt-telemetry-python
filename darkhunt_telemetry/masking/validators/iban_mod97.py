"""IBAN validator (ISO 13616 mod-97 check).

Strips whitespace, rotates the country+check-digit prefix to the end,
substitutes letters as A=10..Z=35, and verifies that the resulting integer
mod 97 == 1.
"""

from __future__ import annotations

import re

_WS = re.compile(r"\s")


def iban_mod97(input: str) -> bool:
    iban = _WS.sub("", input)
    length = len(iban)
    if length < 15 or length > 34:
        return False

    rearranged = []
    for i in range(4, length + 4):
        c = ord(iban[i % length])
        if 48 <= c <= 57:
            rearranged.append(chr(c))
        elif 65 <= c <= 90:
            rearranged.append(str(c - 65 + 10))
        elif 97 <= c <= 122:
            rearranged.append(str(c - 97 + 10))
        else:
            return False

    try:
        return int("".join(rearranged)) % 97 == 1
    except ValueError:
        return False
