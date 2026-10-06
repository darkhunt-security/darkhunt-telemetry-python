"""Guard configuration: constructor arguments > environment > defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable, Dict, Literal, Optional

if TYPE_CHECKING:
    from .verdict import Verdict

GuardMode = Literal["enforce", "shadow", "off"]
FailMode = Literal["open", "closed"]

DEFAULT_URL = "https://api.darkhunt.ai/guardrail-manager"


def _env(name: str) -> Optional[str]:
    value = os.environ.get(name)
    return value if value else None


def _float(name: str, fallback: float) -> float:
    try:
        return float(_env(name) or fallback)
    except ValueError:
        return fallback


def _headers(raw: Optional[str]) -> Dict[str, str]:
    """``"K1=v1,K2=v2"`` -> ``{"K1": "v1", "K2": "v2"}``."""
    out: Dict[str, str] = {}
    for pair in (raw or "").split(","):
        key, sep, value = pair.partition("=")
        if sep and key.strip():
            out[key.strip()] = value.strip()
    return out


@dataclass(frozen=True)
class GuardConfig:
    """How guarded tools reach Darkhunt and what they do with its answer.

    ``mode``
        ``enforce`` — a DENY stops the call; ``shadow`` — every call is checked
        and recorded but nothing is stopped; ``off`` — no checks at all.
    ``fail``
        What ``enforce`` does when Darkhunt cannot be reached in time: ``open``
        runs the tool, ``closed`` refuses it. Required in ``enforce`` mode — it is
        a decision about the application's availability that only its owner can
        make.
    ``tenant_id`` / ``workspace_id`` / ``application_id``
        Fallback routing for calls made outside an active trace. Inside one, the
        trace's own routing is used.
    """

    url: str = DEFAULT_URL
    api_key: Optional[str] = None
    tenant_id: Optional[str] = None
    workspace_id: Optional[str] = None
    application_id: Optional[str] = None
    mode: GuardMode = "shadow"
    fail: Optional[FailMode] = None
    call_timeout_s: float = 1.5
    result_timeout_s: float = 5.0
    max_result_bytes: int = 64 * 1024
    headers: Dict[str, str] = field(default_factory=dict)
    source: Optional[str] = None
    on_verdict: "Optional[Callable[[Verdict], None]]" = None

    def __post_init__(self) -> None:
        if self.mode not in ("enforce", "shadow", "off"):
            raise ValueError(
                f"darkhunt guard: mode must be enforce, shadow or off, not {self.mode!r}"
            )
        if self.fail is not None and self.fail not in ("open", "closed"):
            raise ValueError(f"darkhunt guard: fail must be open or closed, not {self.fail!r}")
        if self.mode == "enforce" and self.fail is None:
            raise ValueError(
                "darkhunt guard: enforce mode needs a fail mode — set DARKHUNT_GUARD_FAIL "
                "(or fail=) to 'open' (run the tool when Darkhunt is unreachable) or "
                "'closed' (refuse it)"
            )

    @classmethod
    def from_env(cls, **overrides) -> "GuardConfig":
        values: Dict[str, Any] = dict(
            url=(_env("DARKHUNT_GUARD_URL") or DEFAULT_URL).rstrip("/"),
            api_key=_env("DARKHUNT_API_KEY"),
            tenant_id=_env("DARKHUNT_TENANT_ID"),
            workspace_id=_env("DARKHUNT_WORKSPACE_ID"),
            application_id=_env("DARKHUNT_APPLICATION_ID"),
            mode=(_env("DARKHUNT_GUARD_MODE") or "shadow").lower(),
            fail=(_env("DARKHUNT_GUARD_FAIL") or "").lower() or None,
            call_timeout_s=_float("DARKHUNT_GUARD_TIMEOUT_CALL", 1.5),
            result_timeout_s=_float("DARKHUNT_GUARD_TIMEOUT_RESULT", 5.0),
            max_result_bytes=int(_float("DARKHUNT_GUARD_MAX_RESULT", 64 * 1024)),
            headers=_headers(_env("DARKHUNT_GUARD_HEADERS")),
        )
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)

    def with_(self, **changes) -> "GuardConfig":
        return replace(self, **{k: v for k, v in changes.items() if v is not None})


_config: Optional[GuardConfig] = None


def configure_guard(config: Optional[GuardConfig] = None, **overrides) -> GuardConfig:
    """Set the process-wide guard configuration.

    ``configure_guard(mode="enforce", fail="open", on_verdict=report)`` reads the
    environment and applies the overrides; pass a :class:`GuardConfig` to set it
    outright. Guarded tools read the configuration on every call, so this can be
    called after they are decorated.
    """
    global _config
    _config = config.with_(**overrides) if config is not None else GuardConfig.from_env(**overrides)
    return _config


def get_config() -> GuardConfig:
    global _config
    if _config is None:
        _config = GuardConfig.from_env()
    return _config


def reset_config() -> None:
    """Forget the configuration so the next call re-reads the environment (tests)."""
    global _config
    _config = None
