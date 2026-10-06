# Darkhunt telemetry for Python

[![PyPI version](https://img.shields.io/pypi/v/darkhunt-telemetry.svg)](https://pypi.org/project/darkhunt-telemetry/)
[![Supported Python versions](https://img.shields.io/pypi/pyversions/darkhunt-telemetry.svg)](https://pypi.org/project/darkhunt-telemetry/)
[![CI](https://github.com/darkhunt-security/darkhunt-telemetry-python/actions/workflows/ci.yml/badge.svg)](https://github.com/darkhunt-security/darkhunt-telemetry-python/actions/workflows/ci.yml)
[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=darkhunt-security_darkhunt-telemetry-python&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=darkhunt-security_darkhunt-telemetry-python)
[![Reliability Rating](https://sonarcloud.io/api/project_badges/measure?project=darkhunt-security_darkhunt-telemetry-python&metric=reliability_rating)](https://sonarcloud.io/summary/new_code?id=darkhunt-security_darkhunt-telemetry-python)
[![Security Rating](https://sonarcloud.io/api/project_badges/measure?project=darkhunt-security_darkhunt-telemetry-python&metric=security_rating)](https://sonarcloud.io/summary/new_code?id=darkhunt-security_darkhunt-telemetry-python)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Checked with mypy](https://www.mypy-lang.org/static/mypy_badge.svg)](https://mypy-lang.org/)
[![License](https://img.shields.io/pypi/l/darkhunt-telemetry.svg)](https://github.com/darkhunt-security/darkhunt-telemetry-python/blob/main/LICENSE)

Python SDK for sending LLM **traces**, **generations**, and **observations** to
the Darkhunt platform for persistence and security data enrichment. Built on
OpenTelemetry primitives (TracerProvider, BatchSpanProcessor, OTLP/protobuf).

This is the Python analog of
[`@darkhunt-security/telemetry`](https://github.com/darkhunt-security/darkhunt-telemetry-ts)
(the TypeScript SDK) — same wire contract and routing semantics, adapted to
Python idioms (keyword arguments, `with` context managers).

- **`DarkhuntTelemetry`** — the client. One per process.
- **`Trace`** — a single user-facing interaction. Carries routing fields.
- **`Generation`** — one LLM round-trip under a trace (`model`, messages, `usage`, `cost`).
- **`Span`** — anything else (tool calls, retrievals, guardrails, sub-agents).
- **`@guard`** — asks Darkhunt before a tool runs and before its output is used,
  so dashboard rules can block it ([Guard tool calls](#guard-tool-calls-guard);
  optionally [through Microsoft AGT](#microsoft-agent-governance-toolkit-optional-experimental)).
- **`check_input` / `check_output`** — the same check for the request before the
  agent sees it and the answer before the user does
  ([Guard the request and the answer](#guard-the-request-and-the-answer)).

Requires Python **3.9+**.

> **Let Claude Code wire it up for you.** Install the plugin:
>
> ```
> /plugin marketplace add darkhunt-security/darkhunt-telemetry-python
> /plugin install darkhunt-telemetry-py@darkhunt-py
> ```
>
> Then tell Claude _"add Darkhunt telemetry to this service"_ and the [`darkhunt-telemetry-python-integration`](https://github.com/darkhunt-security/darkhunt-telemetry-python/blob/main/plugins/darkhunt-telemetry-py/skills/darkhunt-telemetry-python-integration/SKILL.md) skill auto-invokes and does the steps below for you.

---

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

> Several logical agents in one process? `service_name` is per client, so they would
> share a node — name them per trace with
> [`agent`](#several-agents-in-one-process) instead.

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

| Option (`DarkhuntTelemetry(...)`) | Env var                                       | Default                             |
|-----------------------------------|-----------------------------------------------|-------------------------------------|
| `base_url`                        | `DARKHUNT_BASE_URL`                           | `https://api.darkhunt.ai/trace-hub` |
| `api_key`                         | `DARKHUNT_API_KEY`                            | — (required on the public endpoint) |
| `service_name`                    | `DARKHUNT_SERVICE_NAME` / `OTEL_SERVICE_NAME` | `darkhunt-telemetry`                |
| `tenant_id`                       | `DARKHUNT_TENANT_ID`                          | — (required)                        |
| `workspace_id`                    | `DARKHUNT_WORKSPACE_ID`                       | — (required)                        |
| `application_id`                  | `DARKHUNT_APPLICATION_ID`                     | — (required)                        |
| `assessment_run_id`               | `DARKHUNT_ASSESSMENT_RUN_ID`                  | — (optional, Darkhunt-internal)     |
| `release`                         | `DARKHUNT_RELEASE`                            | —                                   |
| `environment`                     | `DARKHUNT_ENVIRONMENT`                        | —                                   |
| `enabled`                         | `DARKHUNT_ENABLED`                            | `true`                              |
| `internal`                        | `DARKHUNT_INTERNAL`                           | `false`                             |
| `flush_at`                        | `DARKHUNT_FLUSH_AT`                           | `20` spans                          |
| `flush_interval_ms`               | `DARKHUNT_FLUSH_INTERVAL` (seconds)           | `5` s                               |
| `timeout_ms`                      | `DARKHUNT_TIMEOUT` (seconds)                  | `10` s                              |
| `register_context_manager`        | `DARKHUNT_REGISTER_CONTEXT_MANAGER`           | `true`                              |

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

## Data masking

The SDK does **not** mask data client-side: inputs, outputs, messages, system
prompts, metadata, tool arguments, names, tags, and status messages are sent
verbatim. PII masking happens server-side in the Darkhunt platform on ingest.
Server-side masking covers inputs, outputs, messages, system prompts, tool calls, span names and status messages; **metadata values, tags and routing IDs are stored as sent**, so keep secrets and PII out of them.

Routing identifiers (`session_id`, `user_id`, `user_email`) round-trip verbatim
so the dashboard can group and filter by exact match.

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

### Several agents in one process

`service_name` sets the OTel Resource, which is fixed per `TracerProvider` — i.e.
per client. A process hosting several **logical** agents behind one shared client
therefore renders as a single node named after the process.

When that's your shape, name the agent **per trace** instead. One client, one
provider, one Resource:

```python
# One client for the process. Routing stays off it when it varies per request.
dh = DarkhuntTelemetry(service_name="alludium-web")

research = dh.trace(
    "research.run",
    agent="research",             # ← the topology node for this trace
    session_id=sid,               # ← must be shared across the agents in one run
    tenant_id=t, workspace_id=w, application_id=a,
)

scoring = dh.trace(
    "score.deal",
    agent="deal-scoring",
    session_id=sid,               # ← same session
    handoff_from=[research.handoff_token()],
    tenant_id=t, workspace_id=w, application_id=a,
)
```

This composes with [multi-tenant routing](#configuration): `agent` and the routing
fields are independent, so a host serving many customers from one process passes
**both** per trace — `tenant_id` / `workspace_id` / `application_id` from the request
context, `agent` from whichever logical agent is running. Only `service_name` stays
on the client.

`agent` emits `service.name` as a **span** attribute on the root and on every child
span. The backend resolves a trace group's identity from merged attributes, where
span attributes outrank the Resource — so each agent becomes its own node while
`service_name` stays the fallback for traces that don't set it.

Two consequences worth knowing before adopting it:

> **An agent-scoped trace is always a new root.** Identity is resolved once per
> trace id, so two agents sharing a trace would collapse onto whichever name merged
> first. To make that impossible, passing `agent` ignores both `handoff_from[0]` and
> any ambient active span when parenting. `handoff_from` still records every upstream
> as an `agent_handoff` **link**, and links are what the topology walks to draw the
> edge — so the graph is unchanged; the cross-agent parent chain is what you give up.
> (A trace then covers one agent's slice rather than the end-to-end request.)

> **Share `session_id` across the agents in one run.** With the parent chain gone,
> edges come from links alone, and links resolve **within a session**. Different
> sessions, no edge.

Use a small, stable set of values (`"research"`, `"deal-scoring"`) — never a request
id or anything derived from user input. Each distinct value is a permanent topology
node.

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

## Guard tool calls (`@guard`)

`@guard` asks Darkhunt before a tool runs, and before its output is used, so the
rules in the Darkhunt dashboard can **stop** a call, not only record it. Each
guarded call makes up to two checks against the guardrail manager's `/verify`:

- **`TOOL_CALL`**, with the arguments, before the function runs. A block means
  the function never runs.
- **`TOOL_RESULT`**, with what it returned (`after=True`, the default). A block
  means it ran but its output is withheld.

```python
from darkhunt_telemetry.guard import configure_guard, guard

configure_guard(mode="enforce", fail="open")      # or DARKHUNT_GUARD_* env vars

@function_tool          # your framework's decorator on top...
@guard                  # ...guard underneath, so the framework still reads the real signature
def send_referral(to: str, subject: str, message: str) -> dict: ...

with trace.activate():  # the run the checks belong to (session, user, routing)
    result = agent.run(...)
```

It wraps the function itself, so it works with any framework (or none), sync or
async. Each check is recorded as a `guardrail` span under the tool's span, and
reuses the tool span when you already opened one for the same tool.

**What the caller gets on a block (`on_deny`):**

| `on_deny`             | Result                                                                                       | For                                         |
| --------------------- | -------------------------------------------------------------------------------------------- | ------------------------------------------- |
| `"return"` (default)  | a string: *"Blocked by Darkhunt: \<rule\>. The \<tool\> tool was not run."*                    | tools a model calls: it reads the refusal   |
| `"raise"`             | `DarkhuntBlocked(verdict)`                                                                   | code paths that cannot continue             |
| a callable            | its return value, given the `Verdict` (e.g. `lambda v: []`)                                  | pipelines that can continue without the data |

**Modes and failure.** `off` makes no calls. `shadow` (the default) checks and
records but never blocks: a DENY is recorded as *"Would block (shadow)"*.
`enforce` blocks on DENY. When Darkhunt does not answer (timeout, network,
HTTP error), `fail="open"` lets the call through and `fail="closed"` blocks it.
Enforce mode **requires** an explicit `fail`.

| Option (`configure_guard(...)`) | Env var                         | Default                                   |
| ------------------------------- | ------------------------------- | ----------------------------------------- |
| `url`                           | `DARKHUNT_GUARD_URL`            | `https://api.darkhunt.ai/guardrail-manager` |
| `api_key`                       | `DARKHUNT_API_KEY`              | —                                         |
| `tenant_id` / `workspace_id` / `application_id` | `DARKHUNT_TENANT_ID` / … | taken from the active trace first   |
| `mode`                          | `DARKHUNT_GUARD_MODE`           | `shadow`                                  |
| `fail`                          | `DARKHUNT_GUARD_FAIL`           | — (required for `enforce`)                |
| `call_timeout_s`                | `DARKHUNT_GUARD_TIMEOUT_CALL`   | `1.5` s                                   |
| `result_timeout_s`              | `DARKHUNT_GUARD_TIMEOUT_RESULT` | `5.0` s                                   |
| `max_result_bytes`              | `DARKHUNT_GUARD_MAX_RESULT`     | `65536` (larger results are sent truncated) |
| `headers`                       | `DARKHUNT_GUARD_HEADERS`        | — (`K=V,K2=V2`)                            |
| `on_verdict`                    | —                               | — (a hook called with every `Verdict`)    |

Notes:

- **Name tools the way your rules match them:** `@guard(name="db.get_patient")`.
- **Framework context objects are not sent.** `self`, `ctx`, `context`,
  `run_context` and `wrapper` are left out by default (`exclude=`).
- **Call guarded tools inside the run.** `with trace.activate():` makes a trace
  current without ending it. Outside a trace the checks still run on the
  configured routing, but carry no session.
- **Generators are refused.** Streaming tools cannot be guarded yet.

## Guard the request and the answer

`check_input` sends the request to `/verify` at the `INPUT` stage before the agent
sees it, and `check_output` sends the answer at `OUTPUT` before the user does. They
use the same configuration as `@guard`: mode, fail mode, routing, `on_verdict`, and
a `guardrail` span under the current trace. Stopping the work is up to you, since
only your code knows what "don't run the agent" or "don't show the answer" means:

```python
from darkhunt_telemetry.guard import check_input, check_output, refusal

with trace.activate():
    verdict = check_input(request)
    if verdict.blocked:
        return refusal(verdict)  # "Blocked by Darkhunt: <rule>. The request was not processed."
    answer = run_agent(request)
    verdict = check_output(answer)
    return refusal(verdict) if verdict.blocked else answer
```

- **Pass messages for context:** `check_input([{"role": "system", "content": ...}, {"role": "user", "content": ...}])`.
  A plain string is sent as one `user` (input) or `assistant` (output) message.
- **Async:** `await acheck_input(...)` / `await acheck_output(...)`.
- **Budget:** content is classified by a model, so these checks use
  `result_timeout_s`, not the tighter `call_timeout_s`. Text over
  `max_result_bytes` is sent truncated.
- **Act on `verdict.blocked`,** as with tools: a DENY in `shadow` mode is
  `denied` but not `blocked`.

## Microsoft Agent Governance Toolkit (optional, experimental)

If your agents already use Microsoft's
[Agent Governance Toolkit](https://github.com/microsoft/agent-governance-toolkit)
(AGT), Darkhunt can be the policy behind it. AGT's Agent Control Specification
(ACS) runtime stops an agent at intervention points and asks a policy for a
verdict. `DarkhuntPolicy` answers `pre_tool_call` and `post_tool_call` with
`/verify`, so the decisions follow the same dashboard rules, enforcement log and
`guardrail` spans as `@guard`.

```yaml
# agt.yaml
agent_control_specification_version: 0.3.1-beta
metadata: {name: my-agent}
policies:
  darkhunt: {type: custom, adapter: darkhunt}
intervention_points:
  pre_tool_call:  {policy: {id: darkhunt}, policy_target: $.tool_call.args}
  post_tool_call: {policy: {id: darkhunt}, policy_target: $.tool_result}
annotators: {}
```

```python
from agent_control_specification import AgentControl
from darkhunt_telemetry.agt import DarkhuntPolicy, agt_tool, run_governed, check_tool_point

control = AgentControl.from_path("agt.yaml", policy_dispatcher=DarkhuntPolicy())

@function_tool
@agt_tool(control)              # a tool function (sync stays sync)
def get_holdings(household_id: str) -> str: ...

# a loop that dispatches tools by name:
out = await run_governed(control, block.name, block.input, lambda: run(block))

# allow/deny hooks only (e.g. the Claude Agent SDK's PreToolUse / PostToolUse):
refusal = await check_tool_point(control, "pre_tool_call", tool_name, tool_input)
```

What to know:

- **Install AGT yourself, at the supported version.** `DarkhuntPolicy` reads
  the guard configuration above. AGT is not a dependency of this SDK. Install
  the version shipped with AGT's latest official release (v4.1.0), and pin it
  exactly: the spec is a pre-release and may change between versions.

  ```bash
  pip install "agent-control-specification==0.3.1b1"
  ```

  Manifests must declare `agent_control_specification_version: 0.3.1-beta`.
  It needs Python 3.11+ and is a native (Rust) extension: PyPI has a wheel
  for Linux x86-64 only, and elsewhere it builds from source, which needs a
  Rust toolchain.
- **AGT's framework adapters guard a run's input and output, not the tools
  inside it.** Put `agt_tool` / `run_governed` where your tools are dispatched.
- **Hand the run in explicitly.** ACS evaluates policies in a thread pool that
  drops context variables. `agt_tool`, `run_governed` and `check_tool_point`
  pass the current run through for you; with AGT's own adapters, put the session
  in the snapshot's `envelope`.
- **`DarkhuntPolicy` never raises.** ACS turns a dispatcher error into a deny, so
  the plug-in applies your `fail` mode itself.
- **Only `allow`, `deny` and `warn` are mapped.** ACS's `transform` (redact) and
  `escalate` (approval) have no Darkhunt equivalent yet. Observe-only matches
  and shadow-mode DENYs come back as `warn`.

## Development

Uses [uv](https://docs.astral.sh/uv/) for a fast, reproducible dev environment
(pinned by `uv.lock`); the build backend is hatchling.

```bash
uv sync --all-extras          # create .venv from the lockfile (dev + temporal)
uv run pytest                 # tests
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run bandit -c pyproject.toml -r darkhunt_telemetry
uv build                      # sdist + wheel
```

Plain pip works too, if you'd rather not use uv:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,temporal]"
pytest
```

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
