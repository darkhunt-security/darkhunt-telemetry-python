#!/usr/bin/env bash
# Stop hook: run the CI gate locally (mirrors .github/workflows/ci.yml) whenever
# Python sources or tests changed this turn. Blocks the turn (exit 2) on failure
# so broken code isn't handed back; the failing output is fed to the model.
set -uo pipefail

cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null)}" 2>/dev/null || exit 0

# Skip pure-conversation turns: only run when *.py changed (working tree OR staged).
if git diff --quiet -- '*.py' && git diff --cached --quiet -- '*.py'; then
  exit 0
fi

# Mirror CI: sync the env once up front (locked, all extras), then run every
# tool with --no-sync --no-build so `uv run` never resolves/builds packages
# itself (sdist builds execute arbitrary setup scripts). --no-build holds for
# the sync too: every locked dep ships a wheel, and the project itself is not
# installed (--no-install-project) — pytest imports it from the repo root via
# the `pythonpath` setting in pyproject.toml.
out=$(
  {
    uv sync --quiet --locked --all-extras --no-build --no-install-project &&
      uv run --no-sync --no-build ruff check darkhunt_telemetry tests &&
      uv run --no-sync --no-build ruff format --check darkhunt_telemetry tests &&
      uv run --no-sync --no-build mypy &&
      uv run --no-sync --no-build bandit -c pyproject.toml -r darkhunt_telemetry &&
      uv run --no-sync --no-build pytest -q
  } 2>&1
)
if [ $? -ne 0 ]; then
  {
    echo "Local CI gate FAILED — fix before finishing (ruff / mypy / bandit / pytest):"
    echo "$out" | tail -n 40
  } >&2
  exit 2
fi
exit 0
