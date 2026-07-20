---
name: darkhunt-telemetry-python-integration
description: |
  Use this skill when integrating `darkhunt-telemetry` (the Darkhunt trace-hub
  PYTHON SDK, published on PyPI) into a
  Python service. Covers: install (published on PyPI), singleton client setup,
  trace + generation + span emission via the `with`-based active-context helpers,
  backdated `start_time`, graceful shutdown, routing-field discipline (tenant_id /
  workspace_id / application_id), creating an OBSERVABILITY application via the
  Darkhunt MCP, in-cluster vs public ingest, the masking layer, and — the big part
  — multi-agent topology + agent handoffs across every Python transport (an
  in-process contextvars carrier, orchestrator-passed traces, a LangGraph state
  field, the HTTP `traceparent` header, a queue metadata field, and Temporal
  Headers via `HandoffInterceptor` + `child_args`). Auto-invoke when the user asks
  to add LLM tracing/observability to a Python app, send spans to trace-hub, wire
  `DarkhuntTelemetry` / `client.trace()` / `trace.generation()`, or build a
  multi-agent Python system where agents hand off to each other (agent topology).
---

# Darkhunt telemetry Python SDK — integration guide

This skill walks through wiring `darkhunt-telemetry` (the **Python** SDK) into a
Python service. It is the Python analog of the TypeScript
`darkhunt-telemetry-integration` skill — same wire contract, same routing
semantics, same masking ruleset, adapted to Python idioms (keyword arguments,
`with` context managers, `contextvars`).

The patterns below are extracted from `temporal-demo-python`, an internal
six-domain multi-agent reference integration where each domain uses a *different*
orchestration style, so it demonstrates every handoff transport at once. File-level
pointers into it are listed at the end of this skill; if you don't have that repo
checked out, the inline snippets here are self-contained.

For anything the patterns below don't cover, read the SDK's own `README.md` — it
carries the full API reference + masking docs. It ships inside the installed
package and is published at
<https://github.com/darkhunt-security/darkhunt-telemetry-python>.

## What the SDK is

A Darkhunt-specific span exporter built on OpenTelemetry primitives
(`TracerProvider`, `BatchSpanProcessor`, OTLP/protobuf) that ships spans — traces,
LLM generations, tool calls, retrievals, guardrails — to Darkhunt trace-hub.
Routing semantics (`tenant_id` / `workspace_id` / `application_id`) and the
attribute schema are Darkhunt-specific; trace-hub is the only intended receiver.
Built-in client-side masking redacts 66 secret/PII patterns before payloads leave
the process. Requires Python **3.9+**.

Key shapes:

- **`DarkhuntTelemetry`** — the client. One per process, lifetime-of-the-process.
- **`Trace`** — a single user-facing interaction. Carries routing fields.
- **`Generation`** — one LLM round-trip under a trace (`model`, messages, `usage`, `cost`).
- **`Span`** — anything else (tool calls, retrievals, guardrails, sub-agents). Use
  `observation_type` to categorize.

## Step-by-step integration

### 1. Install — from PyPI

The SDK is published on public PyPI as **`darkhunt-telemetry`**. Install it directly:

```bash
pip install "darkhunt-telemetry[temporal]"
# or, with uv:
uv add "darkhunt-telemetry[temporal]"
```

or declare it as a project dependency:

```toml
# pyproject.toml
dependencies = ["darkhunt-telemetry[temporal]"]
```

then `uv sync`. Releases are **continuous** — every merge to the SDK's `main`
publishes `0.5.<build>` to PyPI, so the newest release is always the latest.
Leave the spec unpinned to track latest, or set a floor (`>=0.5.13`) / pin an
exact version for reproducibility.

**Extras:** `[temporal]` pulls in `temporalio` (only needed for the Temporal
handoff interceptors); `[crypto]` adds the vetted Keccak validator. The core
package imports neither, so it loads with zero Temporal/crypto packages installed.

Because it installs from PyPI, **Docker builds just work** — `pip install` /
`uv sync` pulls the wheel inside the image, with no build-context tricks.

**Alternatives (rarely needed):**

- **Local path** — only when hacking on the SDK itself beside your app.
  With uv: `[tool.uv.sources] darkhunt-telemetry = { path = "../darkhunt-telemetry-python", editable = true }`;
  plain pip: `pip install -e "../darkhunt-telemetry-python[temporal]"`. (This is
  the one case that reintroduces the Docker sibling-path trap — a `context: .`
  build can't `COPY` a dep outside the build context; use a named
  `additional_contexts: { dhsdk: ../darkhunt-telemetry-python }` and `COPY --from=dhsdk`.)
- **Git dependency** — to pin an unreleased commit:
  `darkhunt-telemetry @ git+https://github.com/darkhunt-security/darkhunt-telemetry-python@<ref>`.

### 2. Get an API key

A `dh-...` API key is required for public/external ingest (`internal=False` —
app servers / CLIs / workers calling the public endpoint). In-cluster
service-to-service callers using `internal=True` don't need one.

Create one in the dashboard (**app.darkhunt.ai → Settings → Security → API Keys →
+ Create API key**), copy it immediately (shown once), and put it in the
**`DARKHUNT_API_KEY`** env var — the name the SDK reads by default
(`options api_key ?? DARKHUNT_API_KEY`). If it's missing on the public endpoint the
constructor raises `ValueError: api_key is required for the public endpoint`.

> **The key, the base URL, and the tenant must all be the same environment.** A
> `dh-` key is scoped to one environment's tenant. Source `DARKHUNT_API_KEY`,
> `DARKHUNT_BASE_URL`, and `DARKHUNT_TENANT_ID` from the **same** place — enrolling
> the `darkhunt-cli` writes a matched trio to `~/.darkhunt/credentials.json`
> (`{ apiKey, apiBaseUrl, tenantId }`); prefer that set together.

> **Gotcha: `credentials.json apiBaseUrl` has NO `/trace-hub` suffix** (it stores
> the bare host, e.g. `https://api.darkhunt.ai`). The SDK default `base_url` DOES
> include it (`https://api.darkhunt.ai/trace-hub`). If you source `DARKHUNT_BASE_URL`
> from that field, **append `/trace-hub` yourself** — the exporter posts to
> `{base_url}/otlp/t/{tenant_id}/v1/traces`, so dropping it yields 404.

### 2b. Create an OBSERVABILITY application (get the `application_id`)

Every trace needs an `application_id` (a workspace-scoped UUID). **Create a new,
dedicated OBSERVABILITY app** for the integration — do not reuse a random existing
app (its traces would pollute that scope). In a multi-agent system, **one app per
domain / service group** is typical (agents within a domain are told apart by
`service.name`).

**Reach for the Darkhunt MCP first** (it reuses enrolled credentials). Check the
`darkhunt_*` tools are connected, then:

```text
darkhunt_status                # confirm auth + tenant + reachable API
darkhunt_list_workspaces       # → pick your workspaceId (UUID)
darkhunt_create_application    { workspace, name, type: 'OBSERVABILITY', description? }
                               # → returns the NEW application's UUID (use this)
```

- Pass **`type: 'OBSERVABILITY'`** (default is `RED_TEAM`) so the app's **Tracing**
  view is enabled.
- Put each UUID in `DARKHUNT_APP_<DOMAIN>` (or `DARKHUNT_APPLICATION_ID` for a
  single-app service). If MCP + CLI are both unavailable, use the dashboard's
  new-application flow; don't silently curl the REST API.

### 3. Singleton client (process-wide)

**Don't construct `DarkhuntTelemetry` per request.** Each construction spins up a
`TracerProvider` + `BatchSpanProcessor` and registers an `atexit` handler, so a
per-call client leaks resources and prevents batching. `service.name` is a
**per-client** OTel resource, so distinct agent names need distinct clients — in a
multi-agent process, **memoize one client per `(application_id, service_name)`**:

```python
from darkhunt_telemetry import DarkhuntTelemetry

_clients: dict[str, DarkhuntTelemetry] = {}

def client_for(service_name: str, application_id: str) -> DarkhuntTelemetry:
    key = f"{application_id}::{service_name}"
    c = _clients.get(key)
    if c is None:
        c = DarkhuntTelemetry(
            # api_key / base_url / tenant / workspace read from env by default.
            application_id=application_id,
            service_name=service_name,   # the topology node identity
            internal=False,              # public ingest with the dh- bearer key
        )
        _clients[key] = c
    return c
```

### 4. Graceful degradation — never crash a host app without creds

The client **raises** if `api_key` is missing on the public endpoint, and
`client.trace()` **raises** if a routing field is missing. So gate on config
presence and return `None` when unconfigured; call sites use `if trace:` /
`trace.end() if trace else None`:

```python
import os

_REQUIRED = ("DARKHUNT_API_KEY", "DARKHUNT_TENANT_ID", "DARKHUNT_WORKSPACE_ID")
_ENABLED = os.environ.get("DARKHUNT_ENABLED") != "false" and all(os.environ.get(k) for k in _REQUIRED)

def open_agent_trace(domain, agent, *, session_id, user_id, handoff_from=None, input=None):
    if not _ENABLED:
        return None
    app_id = os.environ.get(f"DARKHUNT_APP_{domain.upper()}")
    if not app_id:
        return None
    client = client_for(f"{domain}.{agent}", app_id)
    tokens = [t for t in (handoff_from or []) if t]
    return client.trace(name=f"{domain}.{agent}", session_id=session_id, user_id=user_id,
                        handoff_from=tokens or None, input=input)
```

`temporal-demo-python/src/demo/telemetry.py` is this exact pattern, production-ready
(`open_agent_trace` / `open_gateway_trace` / `trace_chat` / `trace_gen` /
`trace_tool` / `flush_telemetry` / `shutdown_telemetry`). **Copy it as your
starting point.**

### 5. Wire shutdown on signals

Spans batch in the background. The SDK flushes at process exit via `atexit`, but
**signal-driven shutdown (SIGTERM/SIGINT — `docker stop`, `kill`) bypasses
`atexit`**, losing the in-memory batch. Wire it:

```python
# Long-running server:
try:
    uvicorn.run(app, ...)          # blocks; returns on SIGINT/SIGTERM
finally:
    for c in _clients.values():
        c.shutdown()

# One-shot script / per-task on a server:
client.flush()                     # before returning
```

The Temporal `Worker.run()` resolves on shutdown — call `shutdown()` in its
`finally`. For a per-request worker (HTTP/queue consumer), `client.flush()` after
each handled task is cheap insurance since `atexit` won't fire until the process
ends.

### 6. Wrap each LLM call — prefer the `with`-based active form

The Python SDK's active-context helpers are **synchronous `with` blocks**, which
fit a blocking `chat()` perfectly (and still work called from `async` code):

```python
def trace_chat(trace, name, model, input_messages, call):
    if trace is None:
        return call()                                    # no-op passthrough
    with trace.start_active_generation(name, model=model) as gen:
        gen.update(input_messages=input_messages)        # known at start
        res = call()                                     # span is ACTIVE here → real timing
        gen.end(model=model, output_messages=[res.message],
                usage={"input_tokens": res.usage["input_tokens"],
                       "output_tokens": res.usage["output_tokens"]})
        return res

def trace_tool(trace, tool_name, input, run):
    if trace is None:
        return run()
    with trace.start_active_span(tool_name, observation_type="tool",
                                 tool_name=tool_name, input=input) as span:
        out = run()
        span.end(output=out)
        return out
```

- `start_active_generation` / `start_active_span` **time the span automatically**,
  make it the active OTel span (so provider auto-instrumentation nests under it),
  end it on block exit, and mark ERROR on a raised exception. The inner `gen.end()`
  / `span.end()` is idempotent, so calling it yourself to attach the payload is
  fine.
- `update()` is for fields known at start (`input_messages`, `system_instructions`);
  `end()` for fields known at finish (`output_messages`, `usage`, `cost`).
- **Manual form** (streaming, or holding a span open across calls): open it *after*
  work started and **backdate** with `start_time` (epoch **seconds**, captured
  before the call): `gen = trace.generation(name, model=..., start_time=t0)`. Epoch
  seconds, not ms — the Python convention (`time.time()`).
- **You usually don't need `cost`.** trace-hub auto-prices from `model` + `usage`
  for known models. Only set `cost` for custom/unpriced models.

## Routing fields

Every span carries `tenant_id` / `workspace_id` / `application_id`. Set them once
on the client if constant for the process; pass per-trace if multi-tenant
(`client.trace(tenant_id=..., ...)`). The constructor merges
`constructor arg > env var > default`; `client.trace()` raises `ValueError` if any
is still missing. `assessment_run_id` is optional (Darkhunt-internal grouping);
omit for general production tracing.

## `session_id` and `user_id` — set them every time

Technically optional, but **every integration should set them**. Traces sharing a
`session_id` group into one conversation timeline; the policy engine keys per-user
signals off `user_id`. If not known at open, `trace.update(user_id=..., session_id=...)`
once they are — spans created after inherit the values. Routing identifiers are
**not** masked (they round-trip verbatim for exact-match grouping) — hash any
PII-bearing identifier caller-side.

## In-cluster vs public ingest

| Caller                        | `internal`        | Auth                              | URL                                              |
| ----------------------------- | ----------------- | --------------------------------- | ------------------------------------------------ |
| In-cluster service-to-service | `True`            | none (cluster policy gates it)    | `POST {base_url}/internal/t/{tenant}/v1/traces`  |
| External CLI / browser / app  | `False` (default) | `Authorization: Bearer <api_key>` | `POST {base_url}/otlp/t/{tenant}/v1/traces`      |

## Span types — pick the right one

| Work                    | Python API                                                     | `observation_type`    |
| ----------------------- | -------------------------------------------------------------- | --------------------- |
| LLM round-trip          | `trace.generation(name, model=...)`                            | (auto: `generation`)  |
| External tool call      | `trace.span(name, observation_type="tool", tool_name=...)`     | `"tool"`              |
| Vector search/retrieval | `trace.span(name, observation_type="retriever")`              | `"retriever"`         |
| Sub-agent step          | `trace.span(name, observation_type="agent")`                  | `"agent"`             |
| Input/output guardrail  | `trace.span(name, observation_type="guardrail")`              | `"guardrail"`         |
| Embedding               | `trace.span(name, observation_type="embedding")`              | `"embedding"`         |
| Generic work            | `trace.span(name)`                                             | `"span"` (default)    |
| Fire-and-forget marker  | `trace.event(name)`                                            | `"event"`             |

Every factory has a `start_active_*` variant. Spans nest naturally —
`parent.span(...)` / `parent.generation(...)`. For `tool` spans set `tool_name`
(and optionally `tool_call_id` / `tool_arguments`) so the dashboard shows the real
tool, not the generic type.

## Masking (default-on)

66 secret/PII patterns are redacted from inputs/outputs/messages/system
prompts/metadata/tool args before anything leaves the process. Add site patterns
or disable for local synthetic-data dev:

```python
from darkhunt_telemetry import DarkhuntTelemetry, MaskingOptions
from darkhunt_telemetry.masking import CustomPattern

dh = DarkhuntTelemetry(
    tenant_id="t", workspace_id="w", application_id="a",
    mask=MaskingOptions(enabled=True, custom_patterns=[CustomPattern(regex=r"TICKET-\d+", marker="[TICKET]")]),
)
```

## Multi-agent topology & handoffs

When the service is one agent in a multi-agent system, Darkhunt reconstructs the
**agent topology** (who handed off to whom). **Identity: one `service.name` per
agent** — that string is the topology node. The graph is drawn from the
cross-service `parentSpanId` chain, so the whole job is to make each agent's root
trace **nest under its caller**.

### The one thing to get right: nest via `handoff_from`

`client.trace(handoff_from=[caller_token])` makes the agent's root span a **child**
of `handoff_from[0]` (shared `trace_id`, `parentSpanId` set) **and** records an
`agent_handoff` link. Further entries are supplementary links (fan-in). The
caller's token is `trace.handoff_token()` (an opaque W3C `traceparent` string). The
SDK auto-registers the global OTel context manager + W3C propagator on construction,
so there is nothing else to wire — just pass `handoff_from`.

> **Why nesting matters:** ingestion **drops a contentless span** (an agent's root
> often is — its generations/tools are children) **unless** it is a cross-service
> entry (has a `parentSpanId` into another service). Nest → the root is a
> cross-service entry → kept → connected. Don't nest and don't link → the roots are
> dropped → **disconnected islands** (correct only when the agents truly never hand
> off — see the datastore/queue note below).

### Keep the token OUT of business signatures

The token is observability plumbing — never a typed `handoff` parameter on your
domain functions, entry inputs, message/return types, or graph state visible to
business code. Carry it out-of-band, exactly like OTel carries trace context. Which
carrier depends on the topology + transport:

| Topology / transport                | Carrier                                                                                     | Reference file (temporal-demo-python)         |
| ----------------------------------- | ------------------------------------------------------------------------------------------- | --------------------------------------------- |
| **Linear in-process chain**         | a `contextvars.ContextVar` (the `AsyncLocalStorage` analog) — read `current_handoff()`, publish your own | `src/demo/handoff_context.py`, `domains/weather/` |
| **Branching / fan-in in-process**   | **orchestrator-driven**: the coordinator opens each agent trace with an explicit `handoff_from` and passes the `Trace` into the agent | `domains/finance/run.py`                       |
| **In-process graph (LangGraph)**    | a dedicated field in the graph **state** (node writes its token, next node reads it as `handoff_from`) | `domains/banking/graph.py`                     |
| **HTTP**                            | the W3C `traceparent` **header** (out of the body)                                          | `domains/healthcare/http.py`                  |
| **Queue (Redis/Kafka/SQS/…)**       | a dedicated **message metadata field**, kept out of `data`                                  | `domains/devops/bus.py`                       |
| **Temporal**                        | a **Temporal Header** via `HandoffInterceptor` + per-edge `child_args`                       | `domains/security/`                           |

**Linear in-process — the contextvars carrier** (a single "current" slot; perfect
for `a → b → c`, wrong for branches/fan-in):

```python
from contextlib import contextmanager
from contextvars import ContextVar
_h: ContextVar = ContextVar("handoff", default=None)

@contextmanager
def with_handoff(token):
    reset = _h.set(token)
    try: yield
    finally: _h.reset(reset)

def current_handoff(): return _h.get()
def publish_handoff(token): _h.set(token)

# gateway seeds the scope with its root token; each agent reads+publishes:
with with_handoff(root.handoff_token() if root else None):
    plan = plan_task(inp)      # opens its trace w/ handoff_from=[current_handoff()], then publish_handoff(its token)
    weather = geodata(plan)    # nests under coordinator; publishes its own → advisor nests under geodata
    advisor(weather)
```

**Branching / fan-in in-process — orchestrator-driven** (the single ambient slot
can't express a DAG). The coordinator opens every trace with an explicit
`handoff_from` it computes per edge, and passes a `trace` handle into each agent as
a trailing, defaults-`None` telemetry param (the bounded exception to "no tokens in
signatures"):

```python
tok = lambda t: t.handoff_token() if t else None
root = open_gateway_trace("finance", ...);  root_t = tok(root)
coord = atrace("coordinator", [root_t]);     plan_task(inp, rt, coord);  coord_t = tok(coord); coord.end()
research = atrace("research", [coord_t]);     ...
# parallel-with-different-parents + fan-in are now trivial — each trace is explicit:
bull_th = atrace("bull", [research_t]);  bear_th = atrace("bear", [research_t])   # both ← research
pm = atrace("pm", [quant_t, bull_re_t, bear_re_t])   # fan-in of 3 (handoff_from[0] = parent edge)
```

### Carrying the token across a transport — use the SDK helpers

```python
from darkhunt_telemetry.transports import (
    handoff_to_http_headers, handoff_from_http_headers,   # HTTP: traceparent header
    handoff_to_message_meta, handoff_from_message_meta,   # queue: single-parent
    handoffs_from_messages,                               # queue: fan-in (ordered, de-duped)
    TRACEPARENT_HEADER, HANDOFF_MESSAGE_META_KEY,
)

# HTTP producer / consumer:
requests.post(url, headers=handoff_to_http_headers(trace.handoff_token(), base_headers))
token = handoff_from_http_headers(request.headers)        # → client.trace(handoff_from=[token])

# Queue producer / consumer (token in a DEDICATED field, never inside `data`):
xadd(stream, handoff_to_message_meta(trace.handoff_token(), fields))
token = handoff_from_message_meta(parsed_fields)          # single parent
tokens = handoffs_from_messages([m1, m2, m3])             # fan-in → client.trace(handoff_from=tokens)
```

### Temporal — the hardest, and the biggest trap

```python
from temporalio.worker import Worker
from darkhunt_telemetry.temporal import HandoffInterceptor, current_handoff, child_args
```

- **Worker**: register ONE `HandoffInterceptor()` in `interceptors=[...]` — it wires
  BOTH the activity side (exposes the inbound token to `current_handoff()`) and the
  workflow side (propagates / relocates per-edge overrides into the Temporal Header).
- **Activities** hold ALL the telemetry: open `dh.trace(handoff_from=current_handoff()
  or [])`, wrap chat/tools, `trace.end()` in `finally`. `current_handoff()` **works
  inside SYNC activities** run in a `ThreadPoolExecutor` — temporalio copies the
  contextvar context into the worker thread (verified against
  `temporalio.worker._activity`). No async requirement.
- **NEVER put telemetry in workflow code** — it's a deterministic sandbox (no SDK,
  no `dh.trace`). Its only Darkhunt reference is `child_args`, imported from the
  **sandbox-safe** `darkhunt_telemetry.temporal.handoff_header` subpath (pure dict
  helper, no `temporalio` import) inside `with workflow.unsafe.imports_passed_through():`.
- **⚠️ RETURN the token to build the DAG.** The interceptor defaults nest every
  child under the coordinator's inbound token → a **star** (the call graph), not the
  causal DAG. A workflow can neither mint nor read a token (sandboxed); only an
  **activity** produces one. A Temporal Header flows parent→child only — so a child's
  token comes home exactly one way: the **activity's return value**. Add an optional
  `handoff` field to each activity result; the coordinator threads it into the next
  `execute_child_workflow` via `child_args`:

  ```python
  # activity returns its token:
  return {**result, "handoff": trace.handoff_token() if trace else None}

  # coordinator threads it per edge (child_args attaches a hidden override the
  # interceptor relocates to the header and strips before the child sees it):
  def edge(x, upstream): return child_args(x, [t for t in upstream if t])
  plan  = await execute_child_workflow(ReconWf.run,  edge(recon_in,  [coord_tok]))
  spine = plan.get("handoff")
  batch = await asyncio.gather(*[execute_child_workflow(AnalyzerWf.run, edge(a_in, [spine])) for ...])
  a_toks = [b.get("handoff") for b in batch]                 # this round's analyzer tokens
  taint = await execute_child_workflow(TaintWf.run, edge(taint_in, a_toks))   # fan-in = array
  decision = await execute_child_workflow(PlannerWf.run, edge(plan_in, a_toks))
  spine = decision.get("handoff") or spine                   # loop advances the spine
  ```

  Rules of thumb: **fan-in** = pass the array of upstream tokens; a **loop** = later
  rounds thread the downstream stage's token (advance a `spine` var); a **hub** (a
  guardrail gate fired N times) = keep it on the coordinator's token.
  A collection-returning activity (`list[Finding]`) can't carry a `handoff` field —
  **wrap it** in `{"findings": [...], "handoff": ...}`.
- **The gateway→coordinator edge needs a hand-rolled client interceptor** — the SDK
  ships workflow + activity interceptors but NO client interceptor, so
  `client.start_workflow(...)` carries no header on its own. Inject the gateway
  trace's token (from an ambient store the request handler sets):

  ```python
  import temporalio.converter
  from temporalio.client import Client, Interceptor, OutboundInterceptor, StartWorkflowInput
  from darkhunt_telemetry.temporal import HANDOFF_HEADER

  class _GwOut(OutboundInterceptor):
      async def start_workflow(self, input: StartWorkflowInput):
          token = _gateway_handoff.get()   # a contextvars.ContextVar the handler set
          if token:
              input.headers = {**(input.headers or {}),
                               HANDOFF_HEADER: temporalio.converter.default().payload_converter.to_payload([token])}
          return await self.next.start_workflow(input)
  class _GwInt(Interceptor):
      def intercept_client(self, next): return _GwOut(next)
  client = await Client.connect(addr, namespace=ns, interceptors=[_GwInt()])
  ```

### The orchestrator / gateway node

Ingestion retains a trace ROOT even when contentless (it anchors the topology), so a
gateway that only fans work out survives on its own — open the root, put the task on
its `input`, and hand off from `root.handoff_token()` into the first agent:

```python
root = dh.trace(name=f"{domain}.gateway", session_id=task_id, user_id=user_id, input={"task": task})
first_token = root.handoff_token()   # → first agent's handoff_from
```

### Edge rules (each was a real bug)

- **Link to the REAL producing agent, not the orchestrator.** Thread the token
  wherever one agent's output becomes the next agent's input. Linking a downstream
  agent to the *orchestrator* (because it spawned it) draws a plausible-but-WRONG
  graph (e.g. `advisor` linked to `coordinator` instead of downstream of `geodata`).
- **Agent vs Worker**: a node with ≥1 `generation` renders as an **Agent** (model +
  cost); a tools-only node is a **Worker** (no cost). Emit `trace.generation(...)`
  for EVERY real LLM call, even "boilerplate" ones, or their cost never surfaces.
- **Deep repeat loops → self-loops (`↻ ×N`), not per-round back-edges.** For an
  N-round loop over M agents, link every round to the SAME stable upstream so they
  render as clean self-loops; linking each round to the prior round's output emits a
  tangle of back-edges.
- **Small 2-agent cycle (`↺`)** — here a back-edge IS right (a retried step links the
  step it retries; a debate rebuttal links the opposing thesis).
- **Fan-in is `handoff_from=[a, b, c]`** — `[0]` is the parent edge, the rest are
  links.
- **Logical coupling through a datastore/queue ≠ a drawn edge.** Services related
  only through a shared DB / bucket / a queue the token does NOT ride on have no
  `parentSpanId` chain, so they render as **disjoint islands** — frequently correct
  (classic RAG `ingest`→`answer`). **Do NOT synthesize an edge, and tell the user
  explicitly** that connecting them is an *architecture change* (carry a
  `handoff_token()` across the medium), not a telemetry setting.

## Verification

After wiring:

1. **Types/lint**: `uv run python -m compileall src && uv run ruff check .` (or the
   project's checks), and import every entrypoint.
2. **Graceful no-op**: run with `DARKHUNT_ENABLED=false` and confirm the app still
   runs and every helper no-ops.
3. **Server-side auth/routing probe** — the ONLY reliable programmatic check (a clean
   `flush()`/`shutdown()` is NOT proof of ingestion; the BatchSpanProcessor swallows
   export errors). A **400** on an empty body = auth + routing OK; **401** = wrong/
   absent key; **404** = missing `/trace-hub`:

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' -X POST \
     -H "Authorization: Bearer $DARKHUNT_API_KEY" \
     -H 'Content-Type: application/x-protobuf' \
     -H "X-Workspace-Id: $DARKHUNT_WORKSPACE_ID" \
     -H "X-Application-Id: $DARKHUNT_APPLICATION_ID" \
     --data-binary '' \
     "$DARKHUNT_BASE_URL/otlp/t/$DARKHUNT_TENANT_ID/v1/traces"
   ```

4. **Run the REAL instrumented path** and read its node in the dashboard. Verify with
   the same `service.name` as the integration — a probe under a *different*
   `serviceName` mints a **permanent phantom node** (there's no delete-trace API).
5. **The Darkhunt MCP cannot read traces back** — there is no tracing-query tool. So
   the two checks are (1) the curl probe (auth+routing only) and (2) the human
   opening the dashboard. Don't claim you verified ingestion programmatically.

The dashboard should show: one session-grouped trace per interaction; generations
rendered as chat bubbles with `input_messages`/`output_messages`; routing attributes
on the span detail; and token usage / model / computed cost on generations.

## On completion, report the topology shape to the user

Finishing the wiring is not the last step — tell the user what the Topology tab will
(and won't) show, proactively, before they open it. A **connected** graph (real
handoffs wired) → name the edges to expect (`coordinator → geodata → advisor`,
self-loops, fan-in). **Disconnected** nodes (independent processes with no live
handoff — standalone scripts, or a producer/consumer pair coupled only through a
datastore) are a **correct** result — state why (no `parentSpanId` chain) and that
connecting them is an architecture change. For OSS/example repos, persist that note
in the README too.

## Reference files in temporal-demo-python

These paths are inside the internal `temporal-demo-python` reference repo. Skip
this section if you don't have it — every pattern it indexes is already shown
inline above.

- `src/demo/telemetry.py` — the singleton/memoized clients + `open_agent_trace` /
  `open_gateway_trace` / `trace_chat` / `trace_gen` / `trace_tool` / lifecycle.
- `src/demo/handoff_context.py` — the `contextvars` ambient carrier.
- `domains/weather/` — linear in-process (ambient carrier).
- `domains/finance/run.py` — orchestrator-driven DAG (fan-in, `trace_gen` for a
  non-`chat()` LLM path).
- `domains/banking/graph.py` — LangGraph state-threaded handoff.
- `domains/healthcare/{http,handlers,orchestration}.py` — HTTP `traceparent` header +
  consensus fan-in.
- `domains/devops/{bus,handlers,orchestration}.py` — Redis-stream field + rca fan-in.
- `domains/security/{activities,workflows,temporal_worker}.py` + `server.py` —
  Temporal: `HandoffInterceptor`, activities return tokens, coordinator threads
  `child_args` per edge, hand-rolled gateway client interceptor.

## Common pitfalls

1. **Constructing the client per call** → leaks. Memoize per `(application_id,
   service_name)`.
2. **Pinning to an unpublished local checkout when PyPI is fine** → drift from the
   released SDK. Prefer `pip install "darkhunt-telemetry[temporal]"` from PyPI (§1);
   reserve the local-path/git deps for hacking on the SDK or pinning an unreleased ref.
3. **No signal-driven shutdown** → SIGTERM loses the in-memory batch. Wire
   `shutdown()` in a `finally`.
4. **Missing `start_time` on the MANUAL form** → ~0ms duration. Prefer the
   `start_active_*` form (auto-timed); if manual, backdate with `start_time` in epoch
   **seconds**.
5. **`base_url` without `/trace-hub`** → 404 (esp. when sourced from
   `credentials.json apiBaseUrl`, which omits it).
6. **Reusing an existing app / dropping to raw REST.** Create a NEW OBSERVABILITY app
   via the MCP.
7. **Telemetry in Temporal workflow code** → sandbox violation / non-determinism.
   Telemetry lives in activities + the gateway; workflows only reference the
   sandbox-safe `child_args`.
8. **Temporal star instead of the DAG** → you didn't RETURN activity tokens +
   `child_args` them per edge. See the Temporal section.
9. **Disconnected nodes handed over without explanation** → reads as broken. Report
   the topology shape (correct-when-independent) proactively.
10. **Verifying with a throwaway probe under a different `serviceName`** → permanent
    phantom node. Use the curl probe + the real path.
