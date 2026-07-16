"""Shared type aliases mirroring ``src/types.ts``.

These are lightweight typing helpers — the SDK accepts plain ``dict`` values at
runtime, so callers never have to construct ``TypedDict`` instances explicitly.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

try:  # Literal is in typing on 3.8+; fall back defensively.
    from typing import Literal, TypedDict
except ImportError:  # pragma: no cover
    from typing_extensions import Literal, TypedDict  # type: ignore

ObservationType = Literal[
    "span",
    "tool",
    "agent",
    "generation",
    "event",
    "chain",
    "retriever",
    "evaluator",
    "embedding",
    "guardrail",
]

ObservationLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

# The set of valid observation types, used for light runtime documentation.
OBSERVATION_TYPES = (
    "span",
    "tool",
    "agent",
    "generation",
    "event",
    "chain",
    "retriever",
    "evaluator",
    "embedding",
    "guardrail",
)


class Usage(TypedDict, total=False):
    """Token-usage details for a generation. Extra keys are allowed and are
    JSON-encoded into ``darkhunt.observation.usage_details``."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int


class Cost(TypedDict, total=False):
    """Cost details for a generation. ``total`` maps to ``gen_ai.usage.cost``."""

    total: float


class ChatMessage(TypedDict, total=False):
    """A single chat-style message ``{"role": ..., "content": ...}`` for the
    OTel GenAI ``gen_ai.input.messages`` / ``gen_ai.output.messages`` attrs."""

    role: str
    content: str


Metadata = Dict[str, Any]

__all__ = [
    "ObservationType",
    "ObservationLevel",
    "OBSERVATION_TYPES",
    "Usage",
    "Cost",
    "ChatMessage",
    "Metadata",
    "Any",
    "Optional",
]
