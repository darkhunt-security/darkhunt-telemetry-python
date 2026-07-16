"""Optional Temporal transport for Darkhunt handoff propagation.

Requires the optional ``temporalio`` dependency::

    pip install "darkhunt-telemetry[temporal]"

The core package never imports this module, so it loads with zero Temporal
packages installed. Import the interceptor + helpers from here on the worker
side::

    from darkhunt_telemetry.temporal import HandoffInterceptor, current_handoff, child_args
"""

from __future__ import annotations

from .handoff_header import HANDOFF_HEADER, HANDOFF_META, child_args
from .interceptors import HandoffInterceptor, current_handoff

__all__ = [
    "HandoffInterceptor",
    "current_handoff",
    "child_args",
    "HANDOFF_HEADER",
    "HANDOFF_META",
]
