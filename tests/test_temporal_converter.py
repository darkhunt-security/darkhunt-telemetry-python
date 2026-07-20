"""Regression tests for Issue #3 — the activity side must decode the handoff
Temporal Header with the worker's CONFIGURED payload converter, not the global
default. A worker built with a custom ``DataConverter`` encodes the header one
way; decoding with the default converter throws, the exception used to be
silently swallowed, and the handoff token vanished on every activity.

These tests prove:
  * the header round-trips through a NON-default payload converter (unit-level
    and, when a Temporal test server is reachable, through a live worker);
  * decoding the same header with the WRONG (default) converter now surfaces a
    :class:`HandoffHeaderWarning` and returns ``None`` instead of crashing or
    silently dropping the token.
"""

from __future__ import annotations

import asyncio
import uuid
import warnings
from datetime import timedelta
from typing import Optional

import pytest

pytest.importorskip("temporalio")

import temporalio.converter
from temporalio import activity, workflow
from temporalio.api.common.v1 import Payload
from temporalio.common import RetryPolicy
from temporalio.converter import (
    CompositePayloadConverter,
    DataConverter,
    DefaultPayloadConverter,
    JSONPlainPayloadConverter,
)

from darkhunt_telemetry.temporal import HandoffInterceptor, child_args, current_handoff
from darkhunt_telemetry.temporal.handoff_header import HANDOFF_HEADER
from darkhunt_telemetry.temporal.interceptors import (
    HandoffHeaderWarning,
    _decode_header,
    _HandoffActivityInbound,
)

TOKEN = "00-11111111111111111111111111111111-2222222222222222-01"


class _CustomPayloadConverter(CompositePayloadConverter):
    """A worker-configured converter that tags JSON payloads with a non-default
    encoding (``json/darkhunt``). The global default converter has no decoder
    registered for that encoding, so decoding its payloads with the default
    converter raises — exactly the mismatch a real encryption/Pydantic/
    compression ``DataConverter`` produces."""

    def __init__(self) -> None:
        super().__init__(
            *[
                JSONPlainPayloadConverter(encoding="json/darkhunt")
                if isinstance(c, JSONPlainPayloadConverter)
                else c
                for c in DefaultPayloadConverter().converters.values()
            ]
        )


CUSTOM_DATA_CONVERTER = DataConverter(payload_converter_class=_CustomPayloadConverter)


def _encode_header(converter, tokens, key=HANDOFF_HEADER) -> dict:
    """Encode a handoff header the way the workflow-outbound interceptor does."""
    return {key: converter.to_payload(tokens)}


# --------------------------------------------------------------------------- #
# Unit-level: converter mismatch is the bug; configured converter is the fix.  #
# --------------------------------------------------------------------------- #


def test_configured_converter_decodes_but_default_fails():
    """The header encoded by a non-default worker converter round-trips only
    when decoded with that SAME converter. Decoding with the global default
    (the pre-fix activity-side behaviour) fails, warns, and returns None."""
    custom = _CustomPayloadConverter()
    headers = _encode_header(custom, [TOKEN])

    # Fix: decode with the worker's configured converter -> token survives.
    assert _decode_header(headers, custom) == [TOKEN]

    # Bug: decode with the global default -> mismatch -> warn + None (no raise).
    default = temporalio.converter.default().payload_converter
    with pytest.warns(HandoffHeaderWarning):
        assert _decode_header(headers, default) is None


def test_corrupt_header_warns_and_returns_none():
    """A structurally-corrupt payload must warn and return None, never raise —
    a bad header must not take the activity down with it."""
    corrupt = {HANDOFF_HEADER: Payload(metadata={b"encoding": b"json/plain"}, data=b"not-json{")}
    default = temporalio.converter.default().payload_converter
    with pytest.warns(HandoffHeaderWarning):
        assert _decode_header(corrupt, default) is None


def test_unexpected_shape_warns_and_returns_none():
    """A header that decodes to a non-list is unusable; warn + None."""
    default = temporalio.converter.default().payload_converter
    headers = {HANDOFF_HEADER: default.to_payload({"not": "a list"})}
    with pytest.warns(HandoffHeaderWarning):
        assert _decode_header(headers, default) is None


def test_absent_header_returns_none_without_warning():
    default = temporalio.converter.default().payload_converter
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning -> failure
        assert _decode_header({}, default) is None
        assert _decode_header(None, default) is None


def test_custom_header_key_round_trip():
    custom = _CustomPayloadConverter()
    key = "x-darkhunt-handoff-custom"
    headers = _encode_header(custom, [TOKEN], key=key)
    assert _decode_header(headers, custom, key) == [TOKEN]
    # The default key is absent, so nothing is decoded.
    assert _decode_header(headers, custom, HANDOFF_HEADER) is None


# --------------------------------------------------------------------------- #
# Activity-inbound converter resolution (constructor fallback + global default) #
# --------------------------------------------------------------------------- #


def test_activity_inbound_resolves_constructor_converter_outside_activity():
    """Outside an activity context ``temporalio.activity.payload_converter()``
    is unavailable, so the constructor override is used when provided."""
    custom = _CustomPayloadConverter()
    inbound = _HandoffActivityInbound(next=None, payload_converter=custom)
    assert inbound._resolve_converter() is custom


def test_activity_inbound_resolves_global_default_when_nothing_provided():
    inbound = _HandoffActivityInbound(next=None)
    assert inbound._resolve_converter() is temporalio.converter.default().payload_converter


def test_handoff_interceptor_defaults_and_kwargs():
    """Public API stays backward compatible: zero-arg construction works, and
    the new params are keyword-only with safe defaults."""
    default = HandoffInterceptor()
    assert default._header_key == HANDOFF_HEADER
    assert default._payload_converter is None

    configured = HandoffInterceptor(payload_converter=_CustomPayloadConverter(), header_key="x-alt")
    assert configured._header_key == "x-alt"
    assert configured._payload_converter is not None


# --------------------------------------------------------------------------- #
# End-to-end: a live worker with a NON-default converter keeps the token.      #
# Skips gracefully if a Temporal test server cannot be started.                #
# --------------------------------------------------------------------------- #


@activity.defn
async def capture_handoff() -> "Optional[list]":
    """Report the handoff token the activity-inbound interceptor decoded."""
    return current_handoff()


@workflow.defn
class ChildWorkflow:
    @workflow.run
    async def run(self, arg: dict) -> "Optional[list]":
        return await workflow.execute_activity(
            capture_handoff,
            start_to_close_timeout=timedelta(seconds=10),
            # Fail fast rather than retry if the activity ever raises, so a
            # regression surfaces as a quick failure instead of a hang.
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


@workflow.defn
class ParentWorkflow:
    @workflow.run
    async def run(self, token: str) -> "Optional[list]":
        # Hand the token to the child as a per-edge override; the outbound
        # interceptor relocates it into the (non-default-encoded) header.
        return await workflow.execute_child_workflow(
            ChildWorkflow.run, child_args({"noop": 1}, [token])
        )


async def _run_worker(header_key: str):
    from temporalio.client import Client
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker
    from temporalio.worker.workflow_sandbox import (
        SandboxedWorkflowRunner,
        SandboxRestrictions,
    )

    # Pass this test module + the SDK through the workflow sandbox: the sandbox
    # otherwise reimports them, and importing ``darkhunt_telemetry`` drags in
    # ``requests`` (blocked in the sandbox). Both are deterministic here.
    runner = SandboxedWorkflowRunner(
        restrictions=SandboxRestrictions.default.with_passthrough_modules(
            "darkhunt_telemetry", __name__
        )
    )
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        client = await Client.connect(
            env.client.service_client.config.target_host,
            data_converter=CUSTOM_DATA_CONVERTER,
        )
        task_queue = "dh-test-" + uuid.uuid4().hex
        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[ParentWorkflow, ChildWorkflow],
            activities=[capture_handoff],
            interceptors=[HandoffInterceptor(header_key=header_key)],
            workflow_runner=runner,
        ):
            return await client.execute_workflow(
                ParentWorkflow.run,
                TOKEN,
                id="wf-" + uuid.uuid4().hex,
                task_queue=task_queue,
            )
    finally:
        await env.shutdown()


@pytest.mark.parametrize("header_key", [HANDOFF_HEADER, "x-darkhunt-handoff-custom"])
def test_end_to_end_non_default_converter_preserves_token(header_key):
    """Parent -> child -> activity across a worker whose ``DataConverter`` is
    NON-default: the activity still resolves ``current_handoff()`` to the token
    the coordinator handed off, proving the activity side decodes with the
    worker's configured converter (fix #1) for both the default and an
    overridden header key (fix #3)."""
    try:
        result = asyncio.run(_run_worker(header_key))
    except Exception as exc:  # test-server download / bootstrap unavailable
        pytest.skip(f"Temporal test server unavailable: {type(exc).__name__}: {exc}")
    assert result == [TOKEN]


def test_current_handoff_none_outside_activity():
    assert current_handoff() is None
