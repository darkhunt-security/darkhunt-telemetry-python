"""ABA routing number validator: 9 digits, weighted-mod-10 checksum.

Weights ``[3,7,1,3,7,1,3,7,1]`` per the ABA spec.
"""

from __future__ import annotations

import re

_WEIGHTS = (3, 7, 1, 3, 7, 1, 3, 7, 1)
_WS = re.compile(r"\s")


def aba(input: str) -> bool:
    digits = _WS.sub("", input)
    if len(digits) != 9:
        return False
    total = 0
    for i in range(9):
        n = ord(digits[i]) - 48
        if n < 0 or n > 9:
            return False
        total += n * _WEIGHTS[i]
    return total % 10 == 0
