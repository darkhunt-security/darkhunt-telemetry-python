# Darkhunt telemetry for Python

Python SDK for sending LLM **traces**, **generations**, and **observations** to
the Darkhunt platform for persistence and security data enrichment. Built on
OpenTelemetry primitives (TracerProvider, BatchSpanProcessor, OTLP/protobuf),
with built-in **client-side data masking** that redacts 66 secret/PII patterns
before anything leaves your process.

This is the Python analog of
[`@darkhunt-security/telemetry`](https://github.com/darkhunt-security/darkhunt-telemetry-ts)
(the TypeScript SDK) — same wire contract, same routing semantics, same masking
ruleset, adapted to Python idioms (keyword arguments, `with` context managers).

- **`DarkhuntTelemetry`** — the client. One per process.
- **`Trace`** — a single user-facing interaction. Carries routing fields.
- **`Generation`** — one LLM round-trip under a trace (`model`, messages, `usage`, `cost`).
- **`Span`** — anything else (tool calls, retrievals, guardrails, sub-agents).

Requires Python **3.9+**.

## Get started

### 1. Install

```bash
pip install darkhunt-telemetry
# optional Temporal handoff interceptors:
pip install "darkhunt-telemetry[temporal]"
```

### 2. Create a singleton client

Construct **one** client for the lifetime of the process — it spins up a
TracerProvider + batch processor, so a per-request client leaks resources.

```python
from darkhunt_telemetry import DarkhuntTelemetry

# api_key is read from DARKHUNT_API_KEY if omitted.
dh = DarkhuntTelemetry(
    tenant_id="t1",
    workspace_id="ws-1",
    application_id="app-1",
    service_name="my-service",          # OTel service.name — one per process/agent
)
```

In-cluster service-to-service callers post to the permitAll `/internal/...` path
and need no key:

```python
dh = DarkhuntTelemetry(internal=True, tenant_id="t1", workspace_id="ws-1", application_id="app-1")
```

### 3. Wrap your LLM calls

The ergonomic form — `start_active_generation` times the span automatically and
makes it the active OTel span (so provider auto-instrumentation nests under it):

```python
trace = dh.trace("chat", session_id=session_id, user_id=user_id)

with trace.start_active_generation("answer", model="claude-sonnet-5") as gen:
    gen.update(input_messages=[{"role": "user", "content": prompt}])
    reply = call_llm(prompt)                       # span is ACTIVE here — timing is real
    gen.end(
        model="claude-sonnet-5",
        output_messages=[{"role": "assistant", "content": reply.text}],
        usage={"input_tokens": reply.input_tokens, "output_tokens": reply.output_tokens},
    )

trace.end()
```

The manual form still exists for streaming or when you hold a span open across
calls. If you open the generation *after* the work started, backdate it with
`start_time` (epoch **seconds**, e.g. `time.time()` captured before the call):

```python
import time
start = time.time()
reply = call_llm(prompt)                           # work happens first
gen = trace.generation("answer", model="claude-sonnet-5", start_time=start)
gen.update(input_messages=[{"role": "user", "content": prompt}])
gen.end(output_messages=[{"role": "assistant", "content": reply.text}], usage=...)
```

`update()` is for fields known at start; `end()` for fields known at finish.

### 4. Drain the buffer on shutdown

Spans batch in the background. The SDK flushes at process exit (via `atexit`),
but signal-driven shutdown must be wired explicitly:

```python
import signal

def _shutdown(*_):
    dh.shutdown()

signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)
```

For one-shot scripts, `dh.flush()` before returning is enough.

### 5. Verify it worked

A clean `flush()` is **not** proof of ingestion — the batch processor swallows
export errors. Probe the exact ingest endpoint the exporter uses (a **400** on
an empty body = auth + routing OK; **401** = wrong/absent key; **404** = missing
`/trace-hub` in the base URL):

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H "Authorization: Bearer $DARKHUNT_API_KEY" \
  -H 'Content-Type: application/x-protobuf' \
  -H "X-Workspace-Id: $DARKHUNT_WORKSPACE_ID" \
  -H "X-Application-Id: $DARKHUNT_APPLICATION_ID" \
  --data-binary '' \
  "$DARKHUNT_BASE_URL/otlp/t/$DARKHUNT_TENANT_ID/v1/traces"
```

Then open the Darkhunt dashboard and confirm the trace, its generation bubbles,
routing attributes, and token/cost.

## Span types

| Work                          | API                                                          |
| ----------------------------- | ------------------------------------------------------------ |
| LLM round-trip                | `trace.generation(name, model=...)`                          |
| External tool / function call | `trace.span(name, observation_type="tool", tool_name=...)`   |
| Vector search / retrieval     | `trace.span(name, observation_type="retriever")`             |
| Sub-agent step                | `trace.span(name, observation_type="agent")`                 |
| Input/output guardrail        | `trace.span(name, observation_type="guardrail")`             |
| Embedding                     | `trace.span(name, observation_type="embedding")`             |
| Generic work                  | `trace.span(name)` (default `"span"`)                        |
| Fire-and-forget marker        | `trace.event(name)`                                          |

Spans nest naturally — `parent.span(...)` / `parent.generation(...)` makes the
child a child in the trace tree. Every factory also has an active-context
variant: `start_active_span(...)` / `start_active_generation(...)`.

## `session_id` and `user_id` — set them every time

Routing fields are required; `session_id` and `user_id` are technically optional
but **every integration should set them**. They unlock conversation
visualization (traces sharing a `session_id` group into one timeline) and
per-user guardrails / anomaly detection. Set them late with
`trace.update(user_id=..., session_id=...)` if not known at open — all spans
created after inherit the values.

## Configuration

Every option resolves `constructor arg > env var > default`.

| Option (`DarkhuntTelemetry(...)`) | Env var                             | Default                              |
| --------------------------------- | ----------------------------------- | ------------------------------------ |
| `base_url`                        | `DARKHUNT_BASE_URL`                 | `https://api.darkhunt.ai/trace-hub`  |
| `api_key`                         | `DARKHUNT_API_KEY`                  | — (required on the public endpoint)  |
| `service_name`                    | `DARKHUNT_SERVICE_NAME` / `OTEL_SERVICE_NAME` | `darkhunt-telemetry`      |
| `tenant_id`                       | `DARKHUNT_TENANT_ID`                | — (required)                         |
| `workspace_id`                    | `DARKHUNT_WORKSPACE_ID`             | — (required)                         |
| `application_id`                  | `DARKHUNT_APPLICATION_ID`           | — (required)                         |
| `assessment_run_id`               | `DARKHUNT_ASSESSMENT_RUN_ID`        | — (optional, Darkhunt-internal)      |
| `release`                         | `DARKHUNT_RELEASE`                  | —                                    |
| `environment`                     | `DARKHUNT_ENVIRONMENT`              | —                                    |
| `enabled`                         | `DARKHUNT_ENABLED`                  | `true`                               |
| `internal`                        | `DARKHUNT_INTERNAL`                 | `false`                              |
| `flush_at`                        | `DARKHUNT_FLUSH_AT`                 | `20` spans                           |
| `flush_interval_ms`               | `DARKHUNT_FLUSH_INTERVAL` (seconds) | `5` s                                |
| `timeout_ms`                      | `DARKHUNT_TIMEOUT` (seconds)        | `10` s                               |
| `register_context_manager`        | `DARKHUNT_REGISTER_CONTEXT_MANAGER` | `true`                               |
| `mask`                            | —                                   | `MaskingOptions(enabled=True)`       |

> **Two rules when overriding `base_url`:** use the **ingest API host**
> (`api…darkhunt.ai`), not the dashboard, and **keep the `/trace-hub` path** —
> the exporter posts to `{base_url}/otlp/t/{tenant_id}/v1/traces`.

Routing fields can be set once on the client (constant per process) or per-trace
(multi-tenant): `dh.trace(name, tenant_id=req.tenant_id, ...)`. `dh.trace()`
raises `ValueError` if tenant/workspace/application is missing after merging.

> **Note on `register_context_manager`.** Unlike the Node SDK, Python's
> OpenTelemetry context is `contextvars`-based and always active, so span
> nesting works with nothing to register. This option is kept for parity and
> only ensures a global W3C propagator exists for `traceparent` inject/extract.

## Data masking (default-on)

By default the SDK redacts 66 secret/PII patterns (API keys, tokens, emails,
IBANs, credit cards — Luhn/IIN-validated — SSNs, crypto addresses, and more)
from all inputs, outputs, messages, system prompts, metadata values, tool
arguments, and status messages before they leave the process. Same ruleset
(`rules.json`) as the TypeScript SDK.

Routing identifiers (`session_id`, `user_id`, `user_email`) are **not** masked —
they round-trip verbatim so the dashboard can group and filter by exact match.
Hash any PII-bearing identifier caller-side before passing it.

Add site-specific patterns, or disable masking for local dev with synthetic data:

```python
from darkhunt_telemetry import DarkhuntTelemetry, MaskingOptions
from darkhunt_telemetry.masking import CustomPattern

dh = DarkhuntTelemetry(
    tenant_id="t", workspace_id="w", application_id="a",
    mask=MaskingOptions(
        enabled=True,
        custom_patterns=[CustomPattern(regex=r"TICKET-\d+", marker="[TICKET]")],
    ),
)
```

## Multi-agent topology (agent handoffs)

When your service is one agent in a multi-agent system, Darkhunt reconstructs
the **agent topology** — who handed off to whom — from the cross-service span
tree. The safest, lowest-friction way to draw the edges is to **nest each
agent's trace under its caller** by passing the caller's handoff token:

```python
# Upstream agent: expose its entry-span token after doing its work.
trace = dh.trace("research-agent", session_id=sid, user_id=uid)
token = trace.handoff_token()          # opaque W3C traceparent string
# ... work ...
trace.end()

# Downstream agent: nest under the upstream (parent = handoff_from[0]).
trace = dh.trace("analyst-agent", handoff_from=[token], session_id=sid, user_id=uid)
```

`handoff_from[0]` becomes the **parent edge** (the topology arrow) *and* an
`agent_handoff` link; further entries are supplementary links (fan-in). Give
**each agent its own `service_name`** — that string is the topology node.

Carry the token across a transport in its **metadata channel, never the business
payload** (dependency-free helpers included):

```python
from darkhunt_telemetry.transports import (
    handoff_to_http_headers, handoff_from_http_headers,   # HTTP: W3C traceparent header
    handoff_to_message_meta, handoffs_from_messages,      # Queue: out-of-band message metadata
)

# HTTP producer / consumer
requests.post(url, headers=handoff_to_http_headers(trace.handoff_token(), base_headers))
token = handoff_from_http_headers(request.headers)        # -> dh.trace(handoff_from=[token])

# Queue fan-in
tokens = handoffs_from_messages([m1.headers, m2.headers]) # -> dh.trace(handoff_from=tokens)
```

**Temporal** (optional extra): register one interceptor on the worker; each
activity reads its upstream via `current_handoff()`.

```python
from temporalio.worker import Worker
from darkhunt_telemetry.temporal import HandoffInterceptor, current_handoff, child_args

worker = Worker(client, task_queue="q", workflows=[...], activities=[...],
                interceptors=[HandoffInterceptor()])

# inside an activity:
trace = dh.trace("recon-agent", handoff_from=current_handoff() or [], session_id=task_id)
```

The token rides a **Temporal Header** (out of the business args). A coordinator
authors a per-edge override with `child_args(input_dict, [upstream_token])`;
the interceptor relocates it to the header and strips it before the child sees
it. **Never instrument workflow code** (deterministic sandbox) — telemetry lives
in activities.

### Why the topology may show separate nodes (and that's correct)

Darkhunt draws edges from the cross-service `parentSpanId` chain. Services that
are independent processes with **no live agent→agent handoff** — a repo of
standalone scripts, or a producer/consumer pair coupled only through a datastore
or batch boundary (classic RAG `ingest`→`answer`) — have no such chain, so they
render as **separate, unconnected nodes**. That's the honest picture, not a
misconfiguration. Connecting them is an *architecture change* (carry a
`handoff_token()` across the boundary), not a telemetry setting.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,temporal]"
pytest
```

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
