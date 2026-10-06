"""The ``/verify`` client: one POST per check, no retries.

A retry would spend the latency budget of the tool call it guards; an answer that
does not arrive in time is handled by the fail mode instead.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

import requests

from .config import GuardConfig
from .verdict import RuleMatch, Verdict


def _rules(raw: Any) -> Tuple[RuleMatch, ...]:
    out = []
    for r in raw or ():
        if isinstance(r, dict):
            out.append(
                RuleMatch(
                    rule_id=str(r.get("ruleId", "")),
                    rule_name=str(r.get("ruleName", "")),
                    action=str(r.get("action", "")),
                )
            )
    return tuple(out)


class VerifyClient:
    def __init__(self) -> None:
        # One pooled session per process: a guarded agent makes many small calls,
        # and a fresh TLS handshake each time would dominate their latency.
        self._session = requests.Session()

    def verify(
        self,
        config: GuardConfig,
        *,
        tenant_id: str,
        body: Dict[str, Any],
        timeout_s: float,
    ) -> Verdict:
        tool = (body.get("tool") or {}).get("name", "")
        stage = body["stage"]
        headers = {"Content-Type": "application/json", **config.headers}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        url = f"{config.url}/api/t/{tenant_id}/verify"
        start = time.perf_counter()

        def unanswered(error: str) -> Verdict:
            return Verdict(
                tool=tool,
                stage=stage,
                decision=None,
                error=error,
                latency_ms=(time.perf_counter() - start) * 1000,
                session_id=body.get("sessionId"),
            )

        try:
            resp = self._session.post(url, json=body, headers=headers, timeout=timeout_s)
        except requests.Timeout:
            return unanswered(f"timeout after {timeout_s:g}s")
        except requests.RequestException as err:
            return unanswered(type(err).__name__)
        if resp.status_code != 200:
            return unanswered(f"HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            return unanswered("unreadable response")
        decision: Optional[str] = data.get("decision")
        if decision not in ("ALLOW", "DENY"):
            return unanswered("unreadable response")
        return Verdict(
            tool=tool,
            stage=stage,
            decision=decision,
            matched_rules=_rules(data.get("matchedRules")),
            observed_rules=_rules(data.get("observedRules")),
            failed=bool(data.get("failed")),
            latency_ms=(time.perf_counter() - start) * 1000,
            session_id=body.get("sessionId"),
        )
