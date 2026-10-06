"""Inline enforcement for agent tools — see :func:`guard`.

    from darkhunt_telemetry.guard import guard

    @guard
    def send_referral(patient_id: str, to: str) -> str: ...

Configured from the environment (``DARKHUNT_GUARD_MODE``, ``DARKHUNT_GUARD_FAIL``,
``DARKHUNT_GUARD_URL`` and the usual ``DARKHUNT_*`` routing) or with
:func:`configure_guard`.
"""

from .config import FailMode, GuardConfig, GuardMode, configure_guard, get_config, reset_config
from .decorator import guard
from .verdict import DarkhuntBlocked, RuleMatch, Verdict

__all__ = [
    "guard",
    "configure_guard",
    "get_config",
    "reset_config",
    "GuardConfig",
    "GuardMode",
    "FailMode",
    "Verdict",
    "RuleMatch",
    "DarkhuntBlocked",
]
