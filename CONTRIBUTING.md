# Contributing

Thanks for your interest in contributing. This guide covers the basics.

## Quick start

Uses [uv](https://docs.astral.sh/uv/) for a fast, reproducible dev environment.

```bash
git clone https://github.com/darkhunt-security/darkhunt-telemetry-python
cd darkhunt-telemetry-python
uv sync --all-extras     # create .venv from uv.lock (dev + temporal + crypto)
uv run mypy
uv run pytest
```

That's the full local loop. If those pass, your environment is set up. Plain
`pip` works too: `python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev,temporal]"`.

## What we accept

| Welcome                                                                    | Out of scope                                                                                       |
| ------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| Bug fixes with a regression test                                          | Reformatting / re-styling unrelated code (use a separate PR)                                        |
| New validators in `darkhunt_telemetry/masking/validators/` (one pure function per file, positive + negative tests) | New backends — the SDK speaks vanilla OTLP; backend-specific extensions belong in adapter packages |
| Documentation improvements                                                | Sweeping refactors without a discussed motivation — file an issue first                            |
| Performance fixes with before/after measurements                          | Adding heavyweight runtime dependencies (each new dep affects every consumer)                       |
| Improvements to the multi-agent / transport helpers                       | Removing tests to make CI green                                                                     |

For anything non-trivial, **open an issue first** to discuss the approach. Saves both sides effort if the answer is "we're going a different direction."

## Cross-SDK parity

This SDK is the Python analog of [`@darkhunt-security/telemetry`](https://github.com/darkhunt-security/darkhunt-telemetry-ts) (TypeScript). Both must stay wire-compatible:

- **Attribute keys** (`darkhunt_telemetry/attributes.py`) are the wire contract read by trace-hub. Do not change them without changing the TS SDK and the backend in lockstep.
- **The masking ruleset** (`darkhunt_telemetry/masking/rules/rules.json`) is **mirrored from the TS SDK**, which is the source of truth. New masking rules land in the TS repo's YAML first, then the regenerated `rules.json` is synced here. A parity test (`tests/test_masking_parity.py`) guards ECMAScript regex semantics (`re.ASCII`).

## Pull request checklist

Before pushing, run the same gate CI runs:

```bash
uv run ruff check darkhunt_telemetry tests        # lint
uv run ruff format --check darkhunt_telemetry tests  # formatting
uv run mypy                                         # type-check
uv run bandit -c pyproject.toml -r darkhunt_telemetry # security lint
uv run pytest -q                                    # full suite (3.9–3.13 in CI)
```

Your PR template (`.github/PULL_REQUEST_TEMPLATE.md`) has the full checklist.

## Code style

- **Typed public API** — the package ships `py.typed`; keep everything annotated and mypy-clean. No bare `Any` without a comment explaining why.
- **Comments say _why_, not _what_** — the code says what.
- **One validator per file** under `darkhunt_telemetry/masking/validators/`, a pure function, with positive + negative test cases in `tests/`.
- **Python 3.9 floor** — annotations are stringized via `from __future__ import annotations`; avoid PEP 604 unions (`X | Y`) in any annotation a runtime library will evaluate (e.g. Temporal `@activity.defn` calls `get_type_hints`). Use `Optional[X]`.
- **No emojis in source files or commit messages** unless the situation genuinely calls for one.

## Releasing (maintainers only)

Releases are continuous: **every merge to `main` publishes to PyPI**. CI computes `MAJOR.MINOR.<run_number>` (e.g. `0.5.42`), stamps it into `darkhunt_telemetry/_version.py`, and publishes via PyPI Trusted Publishing (OIDC — no stored token). To start a new minor series (e.g. for breaking changes), bump `MAJOR.MINOR` in `_version.py` on a PR; CI owns the patch segment.

## Reporting security issues

**Don't open a public issue for security vulnerabilities.** See [`SECURITY.md`](./SECURITY.md) for the disclosure policy.

## Code of conduct

This project follows the [Contributor Covenant 2.1](./CODE_OF_CONDUCT.md). Be kind, be patient, assume good faith.
