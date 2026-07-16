"""Luhn (mod-10) checksum validator."""

from __future__ import annotations


def luhn(input: str) -> bool:
    """Return True when the digits-only projection of ``input`` (spaces and
    dashes stripped) passes the Luhn algorithm. Used as the post-match
    validator for the ``luhn`` masking rule and as a building block for the
    credit-card validator."""
    total = 0
    alternate = False
    length = 0
    for ch in reversed(input):
        if ch in (" ", "-"):
            continue
        if not ch.isdigit():
            return False
        n = ord(ch) - 48
        if alternate:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        alternate = not alternate
        length += 1
    return length > 0 and total % 10 == 0
