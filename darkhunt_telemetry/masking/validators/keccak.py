"""Minimal pure-Python Keccak-256 (the pre-NIST padding used by Ethereum).

Vendored so the EIP-55 validator has no third-party crypto dependency.
``hashlib.sha3_256`` is *not* usable here — it implements the final NIST
SHA3-256 padding (``0x06``), whereas Ethereum uses original Keccak padding
(``0x01``). This is a straightforward, well-known implementation of the Keccak
sponge with rate 1088 / capacity 512.
"""

from __future__ import annotations

_ROUND_CONSTANTS = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A,
    0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
    0x8000000080008081, 0x8000000000008009, 0x000000000000008A,
    0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
    0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
    0x000000000000800A, 0x800000008000000A, 0x8000000080008081,
    0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]

_ROTATION_OFFSETS = [
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]

_MASK = (1 << 64) - 1


def _rotl(x: int, n: int) -> int:
    return ((x << n) | (x >> (64 - n))) & _MASK


def _keccak_f(state):
    for rc in _ROUND_CONSTANTS:
        # Theta
        c = [state[x][0] ^ state[x][1] ^ state[x][2] ^ state[x][3] ^ state[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rotl(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x][y] ^= d[x]
        # Rho + Pi
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rotl(state[x][y], _ROTATION_OFFSETS[x][y])
        # Chi
        for x in range(5):
            for y in range(5):
                state[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y])
        # Iota
        state[0][0] ^= rc
    return state


def keccak_256(data: bytes) -> bytes:
    rate_bytes = 136  # 1088-bit rate for Keccak-256
    # Pad: original Keccak padding (0x01 ... 0x80).
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate_bytes != 0:
        padded.append(0x00)
    padded[-1] ^= 0x80

    state = [[0] * 5 for _ in range(5)]
    for offset in range(0, len(padded), rate_bytes):
        block = padded[offset:offset + rate_bytes]
        for i in range(rate_bytes // 8):
            lane = int.from_bytes(block[i * 8:i * 8 + 8], "little")
            state[i % 5][i // 5] ^= lane
        _keccak_f(state)

    out = bytearray()
    while len(out) < 32:
        for i in range(rate_bytes // 8):
            out += state[i % 5][i // 5].to_bytes(8, "little")
            if len(out) >= 32:
                break
        if len(out) < 32:  # pragma: no cover - never needed for 32-byte output
            _keccak_f(state)
    return bytes(out[:32])
