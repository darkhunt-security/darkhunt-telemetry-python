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
