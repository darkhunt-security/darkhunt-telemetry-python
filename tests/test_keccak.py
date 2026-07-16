"""Spec test-vectors locking Keccak-256 correctness (Ethereum padding).

These are Keccak-256 values (original ``0x01`` padding), *not* NIST SHA3-256.
They guard the security decision in ``eip55.py`` (whether to redact an
Ethereum address), for both the vetted ``pycryptodome`` backend and the
vendored pure-Python fallback.
"""

from __future__ import annotations

import importlib

import pytest

from darkhunt_telemetry.masking.validators import keccak as keccak_mod
from darkhunt_telemetry.masking.validators.eip55 import eip55
from darkhunt_telemetry.masking.validators.keccak import keccak_256

# (input, expected Keccak-256 digest) spec vectors.
VECTORS = [
    (b"", "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"),
    (b"abc", "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"),
    # Longer input spanning past a single sponge block boundary.
    (
        b"The quick brown fox jumps over the lazy dog",
        "4d741b6f1eb29cb2a9b9911c82f56fa8d73b04959d3d9d222895df6c0b28aa15",
    ),
]


@pytest.mark.parametrize("data,expected", VECTORS)
def test_keccak_256_spec_vectors(data, expected):
    assert keccak_256(data) == bytes.fromhex(expected)


@pytest.mark.parametrize("data,expected", VECTORS)
def test_pure_python_path_spec_vectors(data, expected):
    """The vendored pure-Python fallback must stay correct on machines
    without pycryptodome — call it directly, bypassing backend selection."""
    assert keccak_mod._keccak_256_pure(data) == bytes.fromhex(expected)


def test_forced_fallback_matches_vectors(monkeypatch):
    """Force the fallback branch regardless of whether pycryptodome is
    installed, then exercise the public ``keccak_256`` entry point."""
    monkeypatch.setattr(keccak_mod, "_HAVE_VETTED", False)
    for data, expected in VECTORS:
        assert keccak_mod.keccak_256(data) == bytes.fromhex(expected)


def test_not_sha3_256():
    """Regression guard: ensure this is Keccak-256, not NIST SHA3-256."""
    import hashlib

    assert keccak_256(b"abc") != hashlib.sha3_256(b"abc").digest()


@pytest.mark.skipif(
    importlib.util.find_spec("Crypto") is None,
    reason="pycryptodome not installed",
)
def test_vetted_matches_pure():
    """When pycryptodome is available, its output must match the pure path."""
    from Crypto.Hash import keccak as pyc_keccak

    inputs = [
        b"",
        b"abc",
        b"The quick brown fox jumps over the lazy dog",
        b"\x00\x01\x02\x03\x04\x05\x06\x07",
        bytes(range(256)),
        b"a" * 136,  # exactly one rate block after padding boundary
        b"a" * 200,
    ]
    for data in inputs:
        vetted = pyc_keccak.new(digest_bits=256, data=data).digest()
        assert keccak_mod._keccak_256_pure(data) == vetted


def test_eip55_valid_checksum_returns_true():
    # Canonical EIP-55 mixed-case checksummed address.
    assert eip55("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed") is True


def test_eip55_corrupted_checksum_returns_false():
    # Same address with one nibble's case flipped -> invalid checksum.
    assert eip55("0x5AAeb6053F3E94C9b9A09f33669435E7Ef1BeAed") is False
