"""What Darkhunt said about one step, and what the guard did about it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

Stage = str  # "TOOL_CALL" | "TOOL_RESULT"


@dataclass(frozen=True)
class RuleMatch:
    rule_id: str
    rule_name: str
    action: str


@dataclass(frozen=True)
class Verdict:
    """One ``/verify`` answer.

    ``decision`` is ``None`` when there was no answer — a timeout, a network
    error or an error status; ``error`` then says which. ``blocked`` is what the
    guard did with it under the configured mode and fail mode, and is the field
    to act on: a DENY in shadow mode is ``denied`` but not ``blocked``.
    """

    tool: str
    stage: Stage
    decision: Optional[str]
    matched_rules: Tuple[RuleMatch, ...] = ()
    observed_rules: Tuple[RuleMatch, ...] = ()
    failed: bool = False
    error: Optional[str] = None
    latency_ms: float = 0.0
    session_id: Optional[str] = None
    mode: str = "shadow"
    blocked: bool = False

    @property
    def denied(self) -> bool:
        return self.decision == "DENY"

    @property
    def unanswered(self) -> bool:
        return self.decision is None

    @property
    def reason(self) -> str:
        """The rule that decided it, or why there was no decision."""
        if self.matched_rules:
            return self.matched_rules[0].rule_name
        if self.error:
            return f"Darkhunt unavailable ({self.error})"
        if self.failed:
            return "Darkhunt could not classify this in time"
        return ""


class DarkhuntBlocked(Exception):
    """Raised by a guarded tool declared with ``on_deny="raise"``."""

    def __init__(self, verdict: Verdict) -> None:
        self.verdict = verdict
        where = "before it ran" if verdict.stage == "TOOL_CALL" else "after it ran; output withheld"
        super().__init__(f"{verdict.tool} blocked by Darkhunt {where}: {verdict.reason}")
