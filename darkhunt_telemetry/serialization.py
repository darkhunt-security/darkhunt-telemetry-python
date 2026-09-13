"""JSON serialization for span attribute values.

OTel attributes only hold primitives, so structured values (messages, tool
arguments, metadata, usage/cost) are stored as JSON strings.
"""

from __future__ import annotations

import json
import warnings
from typing import Any


def safe_json_dumps(value: Any) -> str:
    """json.dumps wrapper that returns a placeholder rather than raising on
    circular refs or other unserializable values — the caller is a span-attribute
    setter on a hot path and must not fail."""
    try:
        return json.dumps(value, default=_json_default, ensure_ascii=False)
    except (TypeError, ValueError) as err:
        warnings.warn(f"darkhunt-telemetry: failed to JSON-encode value: {err}", stacklevel=2)
        return f"[unserializable: {err}]"


def _json_default(value: Any) -> str:
    return str(value)


__all__ = ["safe_json_dumps"]
