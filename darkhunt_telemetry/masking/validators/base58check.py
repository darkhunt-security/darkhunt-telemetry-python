"""Base58Check validator (Bitcoin P2PKH/P2SH-style addresses).

Decodes the Base58 string, splits the trailing 4-byte checksum, and verifies
that the first 4 bytes of double-SHA-256 over the payload match.
"""

from __future__ import annotations

import hashlib

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}
_BASE = 58


def base58check(input: str) -> bool:
    if len(input) == 0:
        return False

    # Count leading '1's — each maps to a leading zero byte after decoding.
    leading_ones = 0
    while leading_ones < len(input) and input[leading_ones] == "1":
        leading_ones += 1

    # Decode as base58 -> integer.
    num = 0
    for ch in input:
        idx = _INDEX.get(ch)
        if idx is None:
            return False
        num = num * _BASE + idx

    # integer -> big-endian bytes.
    body = bytearray()
    while num > 0:
        body.insert(0, num & 0xFF)
        num >>= 8
    data = bytes(leading_ones) + bytes(body)

    if len(data) < 5:
        return False

    payload = data[:-4]
    checksum = data[-4:]
    digest = hashlib.sha256(hashlib.sha256(payload).digest()).digest()
    return digest[:4] == checksum
