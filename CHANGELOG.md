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
