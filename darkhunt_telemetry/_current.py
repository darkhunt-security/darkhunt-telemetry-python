"""The current observation — which trace or span code is running inside.

OTel's active context says which *OTel* span is current, but not which Darkhunt
object owns it, and code that runs deep inside a framework (a tool function the
framework calls) has no other way to find the run it belongs to. This module
keeps that answer in a ``ContextVar``, so it follows the code into asyncio tasks
and into threads started with a copied context (``asyncio.to_thread``,
``contextvars.copy_context().run``).

It is set by :meth:`~darkhunt_telemetry.trace.Trace.activate` and by the
``start_active_*`` helpers, and read by the guard.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Iterator, Optional

if TYPE_CHECKING:
    from .span import ActiveChildHost

_current: "ContextVar[Optional[ActiveChildHost]]" = ContextVar(
    "darkhunt_current_observation", default=None
)


def current_observation() -> "Optional[ActiveChildHost]":
    """The innermost active Darkhunt trace or span, or ``None`` outside one."""
    return _current.get()


@contextmanager
def use_observation(host: "ActiveChildHost") -> Iterator[None]:
    token = _current.set(host)
    try:
        yield
    finally:
        _current.reset(token)


__all__ = ["current_observation", "use_observation"]
