"""Inline enforcement for agents — see :func:`guard` for tools, and
:func:`check_input` / :func:`check_output` for the request and the answer.

    from darkhunt_telemetry.guard import guard

    @guard
    def send_referral(patient_id: str, to: str) -> str: ...

Configured from the environment (``DARKHUNT_GUARD_MODE``, ``DARKHUNT_GUARD_FAIL``,
``DARKHUNT_GUARD_URL`` and the usual ``DARKHUNT_*`` routing) or with
:func:`configure_guard`.
"""

from .config import FailMode, GuardConfig, GuardMode, configure_guard, get_config, reset_config
from .content import acheck_input, acheck_output, check_input, check_output
from .decorator import guard
from .verdict import DarkhuntBlocked, RuleMatch, Verdict, refusal

__all__ = [
    "guard",
    "check_input",
    "check_output",
    "acheck_input",
    "acheck_output",
    "refusal",
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
