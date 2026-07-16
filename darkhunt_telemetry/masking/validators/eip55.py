"""Ethereum address checksum (EIP-55).

Accepts well-formed ``0x``-prefixed 40-hex addresses that are either all
lowercase, all uppercase, or mixed-case where the case pattern matches
``keccak256(lowercase-address)`` per EIP-55.
"""

from __future__ import annotations

import re

from .keccak import keccak_256

_ADDR_RE = re.compile(r"^0[xX][0-9a-fA-F]{40}$")


def eip55(input: str) -> bool:
    if not _ADDR_RE.match(input):
        return False
    addr = input[2:]
    lower = addr.lower()
    # All lowercase or all uppercase has no case pattern to verify per EIP-55.
    if addr == lower or addr == addr.upper():
        return True
    return _matches_checksum(addr, lower)


def _matches_checksum(addr: str, lower: str) -> bool:
    """For each hex char, verify its case matches the keccak256(lower) bit."""
    hash_bytes = keccak_256(lower.encode("ascii"))
    for i in range(40):
        if not _char_case_matches_nibble(ord(addr[i]), _nibble_at(hash_bytes, i)):
            return False
    return True


def _nibble_at(data: bytes, i: int) -> int:
    """i-th hex nibble (i=0 -> high nibble of byte 0, i=1 -> low nibble, ...)."""
    return (data[i >> 1] >> (4 if i % 2 == 0 else 0)) & 0x0F


def _char_case_matches_nibble(ch: int, nibble: int) -> bool:
    """EIP-55 case rule for one position:
    - hex letter a-f -> nibble must be < 8 (hash bit clear => lowercase)
    - hex letter A-F -> nibble must be >= 8 (hash bit set => uppercase)
    - digit 0-9      -> no case constraint
    """
    if 97 <= ch <= 102:  # a-f
        return nibble < 8
    if 65 <= ch <= 70:  # A-F
        return nibble >= 8
    return True
