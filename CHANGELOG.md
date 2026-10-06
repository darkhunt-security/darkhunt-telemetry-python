# Changelog

All notable changes to `darkhunt-telemetry` (the Python SDK) are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Versioning model

Releases are **continuous**: every merge to `main` publishes `MAJOR.MINOR.<run_number>`
to [PyPI](https://pypi.org/project/darkhunt-telemetry/) — the `MAJOR.MINOR` is
controlled in `darkhunt_telemetry/_version.py`; CI owns the patch segment. Because
patch numbers come from the CI run number, they are monotonic but not contiguous.
This changelog groups notable changes by `MAJOR.MINOR` series rather than by every
published patch.

## [Unreleased]

### Added

- **`@guard` — Darkhunt enforcement on tool calls** (`darkhunt_telemetry.guard`).
  - **The checks:** a guarded function is checked against the guardrail
    manager's `/verify` before it runs (`TOOL_CALL`) and before its output is used
    (`TOOL_RESULT`), so dashboard rules can block the call or withhold the result.
  - **Behaviour:** framework-agnostic, sync and async. Modes are
    `off` / `shadow` / `enforce`, with an explicit `fail="open" | "closed"`.
  - **On a block** (`on_deny`): return a refusal string, raise
    `DarkhuntBlocked`, or call your own function.
  - **Recording:** each check is a `guardrail` span under the tool's span.
  - **Configuration:** `configure_guard()` or `DARKHUNT_GUARD_*`.
- **`Trace.activate()`** makes a trace the current run for a block of code
  without ending it, and **`current_observation()`** returns that run. The
  `start_active_*` helpers now also set the current observation.
- **Microsoft Agent Governance Toolkit (AGT) plug-in** (`darkhunt_telemetry.agt`, optional, experimental).
  - **`DarkhuntPolicy`:** an ACS `custom` policy (`adapter: darkhunt`) that
    decides `pre_tool_call` / `post_tool_call` with `/verify`.
  - **Entry points:**
    - `agt_tool` for a tool function;
    - `run_governed` / `run_governed_sync` for loops that dispatch tools by name;
    - `check_tool_point` for allow/deny hooks.
  - **Version:** targets `agent-control-specification==0.3.1b1`, the version in
    AGT's latest official release, v4.1.0 (Python 3.11+). Install it
    separately; it is not a dependency.

### Changed

- **Trace tags, release, environment and metadata are now set on every span**, not
  only the trace root. The root span ends last, so it is usually exported in a later
  batch than its children, and a root-only value never reached them on the backend.
  A span's own metadata still wins on a key the trace also sets.

### Removed

- **BREAKING — client-side data masking is gone; starts the `1.0` series.** PII
  masking now happens server-side in the Darkhunt platform on ingest, so the SDK
  sends every value (inputs, outputs, messages, system instructions, names, tags,
  metadata keys and values, tool fields, status messages) verbatim. Removed from the
  public API, with no deprecation shim:
  - the `mask=` argument on `DarkhuntTelemetry` and the `MaskingOptions` class;
  - `Sanitizer` and `CustomPattern` (top-level exports) and the whole
    `darkhunt_telemetry.masking` package, including its validators and bundled
    `rules.json`. Per-client custom patterns have no replacement;
  - the `sanitizer=` argument and `sanitizer` property on `Trace`, and
    `Trace.mask_name()`;
  - the `crypto` extra (`pycryptodome`), which only served a masking validator.

  Migration: drop any `mask=` / `MaskingOptions` / `CustomPattern` usage.
  `safe_json_dumps` moved from `darkhunt_telemetry.masking` to
  `darkhunt_telemetry.serialization`.

### Added

- **Per-trace agent identity (`agent`).** New `agent` argument on `dh.trace(...)`
  names the **topology node** for that trace, so a process hosting several logical
  agents behind one shared client no longer collapses onto a single node. It emits
  `service.name` as a **span** attribute on the root and every child span; the
  backend resolves a trace group's identity from merged attributes, where span
  attributes outrank the Resource, so `service_name` remains the fallback for traces
  that don't set `agent`. Behaviour is unchanged when it is unset.

  Two behaviours to know: node identity is resolved once per trace id, so an
  agent-scoped trace is deliberately started as a **new root** — it ignores both
  `handoff_from[0]` and any ambient active span when parenting, which makes "two
  agents in one trace" unrepresentable rather than a rule to remember. Upstreams
  stay `agent_handoff` links (what topology reconstruction resolves edges from), so
  the graph is unchanged, but because edges then come from links alone — and links
  resolve **within a session** — every agent in one run must share a `session_id`.

  Matches `agent` in the TypeScript SDK; both emit the same `service.name` key.

- Delivery observability: an optional `on_error` hook on `DarkhuntTelemetry` /
  `DarkhuntSpanExporter` plus exporter counters (`stats()`), so dropped or
  failed-to-export spans are observable instead of silently swallowed.
- Context-manager support on `Span`, `Generation`, and `Trace` (`with` ends the
  span/root on exit; ERROR status on exception).
- Configurable Temporal payload converter and overridable header key on
  `HandoffInterceptor`; decode failures now surface a `HandoffHeaderWarning`.

### Changed

- Exporter retry backoff is now bounded by the export timeout and interruptible
  on shutdown, so a failing tenant can't block the export thread or a flush.
- `DarkhuntTelemetry.flush()` returns `bool` (success) instead of `None`.

### Fixed

- Temporal activity-side handoff decoding now uses the worker's configured
  payload converter instead of the global default, so custom `DataConverter`
  setups no longer silently drop the handoff token.

## [0.5]

Initial public series on PyPI. Python analog of the TypeScript SDK
(`@darkhunt-security/telemetry`) — same wire contract, routing semantics, and
masking ruleset, adapted to Python idioms.
