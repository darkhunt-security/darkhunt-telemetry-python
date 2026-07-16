"""Bech32 / Bech32m validator (BIP-173 / BIP-350).

Decodes the human-readable-part + data + 6-character checksum, runs the
polymod, and accepts either the Bech32 constant (1) or the Bech32m constant
(0x2bc830a3). Mixed-case input is rejected per spec.
"""

from __future__ import annotations

from typing import List

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_GENERATOR = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
_BECH32_CONST = 1
_BECH32M_CONST = 0x2BC830A3


def _polymod(values: List[int]) -> int:
    chk = 1
    for v in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            if (top >> i) & 1:
                chk ^= _GENERATOR[i]
    return chk


def _hrp_expand(hrp: str) -> List[int]:
    out = [ord(c) >> 5 for c in hrp]
    out.append(0)
    out.extend(ord(c) & 31 for c in hrp)
    return out


def bech32(input: str) -> bool:
    if len(input) > 90:
        return False
    lower = input.lower()
    upper = input.upper()
    # Spec: must be all-lowercase or all-uppercase, never mixed.
    if input != lower and input != upper:
        return False

    sep_idx = lower.rfind("1")
    if sep_idx < 1 or sep_idx + 7 > len(lower):
        return False

    hrp = lower[:sep_idx]
    for c in hrp:
        if ord(c) < 33 or ord(c) > 126:
            return False

    data: List[int] = []
    for i in range(sep_idx + 1, len(lower)):
        idx = _CHARSET.find(lower[i])
        if idx == -1:
            return False
        data.append(idx)

    checksum = _polymod(_hrp_expand(hrp) + data)
    return checksum == _BECH32_CONST or checksum == _BECH32M_CONST
