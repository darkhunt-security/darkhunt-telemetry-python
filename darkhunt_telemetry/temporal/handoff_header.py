"""Handoff-over-Temporal-Header — shared constants + a helper.

A Darkhunt handoff token an agent nests under travels in a TEMPORAL HEADER
(out-of-band metadata), NOT in the business args. A coordinator authors a
per-edge choice by attaching it as hidden metadata on a child's input via
:func:`child_args`; the workflow outbound interceptor relocates that to the
header and strips it before the child sees it. Every other hop is
context-propagated (a workflow forwards its own incoming header to its children
and activities).

Pure constants + a dict helper — no ``temporalio`` import, so this is safe to
reference from workflow-sandbox code.
"""

from __future__ import annotations

from typing import List, Mapping, Optional, TypeVar

#: Temporal Header key carrying the handoff token array (a Payload of ``list[str]``).
HANDOFF_HEADER = "x-darkhunt-handoff"

#: Reserved input key a coordinator uses to hand a per-edge override to the
#: outbound interceptor; stripped from the child's args before the wire.
HANDOFF_META = "__dh_handoff"

_T = TypeVar("_T", bound=Mapping)


def child_args(input: _T, handoff_from: Optional[List[str]] = None) -> _T:
    """Return the child-workflow argument, attaching the chosen upstream handoff
    token(s) as hidden metadata. The workflow outbound interceptor moves them
    into the Temporal Header and removes this key, so the child receives ONLY its
    business input.

    ``input`` must be a mapping (dict) — mirroring the TS SDK's ``<T extends
    object>`` contract — because the hidden key is merged into it. With no
    ``handoff_from`` the child inherits the parent workflow's own incoming header
    (plain propagation); the input is returned unchanged.

    Pass the result as the child's argument::

        await workflow.execute_child_workflow(
            MyChild.run, child_args({"task": task}, [upstream_token])
        )
    """
    if not handoff_from:
        return input
    if not isinstance(input, Mapping):
        raise TypeError(
            "child_args(input, handoff_from=[...]) requires a dict-like input so the "
            "per-edge handoff token can be attached; got "
            f"{type(input).__name__}. Use a dict input for edges that need a per-edge "
            "override, or rely on plain header propagation (no handoff_from)."
        )
    merged = dict(input)
    merged[HANDOFF_META] = list(handoff_from)
    return merged  # type: ignore[return-value]


__all__ = ["HANDOFF_HEADER", "HANDOFF_META", "child_args"]
