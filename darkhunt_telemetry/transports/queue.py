"""Carry a Darkhunt handoff token across a QUEUE boundary in the message's
out-of-band metadata — Kafka record headers, SQS message attributes, a Redis
Stream field, etc. — keeping the token OUT of the business payload.

Producing side: attach the token with :func:`handoff_to_message_meta`. Consuming
side: read it back with :func:`handoff_from_message_meta`, or — for a fan-in
worker draining several upstream messages into one downstream trace — collect
the tokens from all of them with :func:`handoffs_from_messages`. Dependency-free.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

#: Stable, namespaced metadata key the handoff token travels under. Lower-case
#: and limited to ``[a-z0-9-]`` so it is a legal key across Kafka header names,
#: SQS message-attribute names, and Redis Stream field names alike.
HANDOFF_MESSAGE_META_KEY = "darkhunt-handoff"


def _meta_value_to_string(value: Any) -> Optional[str]:
    """Best-effort coercion of a transport-specific metadata value to a string.
    Handles plain strings, raw bytes (Kafka header buffers), and SQS-style
    ``{"StringValue": "..."}`` attribute wrappers."""
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, (bytes, bytearray)):
        s = bytes(value).decode("utf-8", errors="replace")
        return s or None
    if isinstance(value, Mapping):
        sv = value.get("StringValue")
        return sv if isinstance(sv, str) and sv else None
    return None


def _read_handoff_key(meta: Mapping[str, Any]) -> Any:
    """Case-insensitive lookup of the handoff key in a metadata map."""
    if HANDOFF_MESSAGE_META_KEY in meta:
        return meta[HANDOFF_MESSAGE_META_KEY]
    for key, value in meta.items():
        if isinstance(key, str) and key.lower() == HANDOFF_MESSAGE_META_KEY:
            return value
    return None


def handoff_to_message_meta(token: str, meta: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Merge a handoff token onto a message's metadata under
    :data:`HANDOFF_MESSAGE_META_KEY`, returning a new dict (input not mutated).
    The value is a plain string — Kafka accepts string header values directly;
    for SQS wrap it as ``{"DataType": "String", "StringValue": token}`` at
    publish time."""
    out: Dict[str, str] = dict(meta) if meta else {}
    out[HANDOFF_MESSAGE_META_KEY] = token
    return out


def handoff_from_message_meta(meta: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Read a single handoff token back out of one message's metadata. Returns
    ``None`` when the key is absent or empty."""
    if not meta:
        return None
    return _meta_value_to_string(_read_handoff_key(meta))


def handoffs_from_messages(metas: List[Optional[Mapping[str, Any]]]) -> List[str]:
    """Fan-in variant: collect the handoff tokens from several upstream
    messages' metadata (skipping any without one), preserving order and
    de-duplicating. Pass the result straight to
    ``client.trace(handoff_from=tokens)``."""
    seen: List[str] = []
    seen_set = set()
    for meta in metas:
        token = handoff_from_message_meta(meta)
        if token and token not in seen_set:
            seen_set.add(token)
            seen.append(token)
    return seen


__all__ = [
    "HANDOFF_MESSAGE_META_KEY",
    "handoff_to_message_meta",
    "handoff_from_message_meta",
    "handoffs_from_messages",
]
