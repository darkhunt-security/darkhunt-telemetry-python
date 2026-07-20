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

# Sync once up front (fast no-op when the lockfile hasn't moved), then run every
# tool with --no-sync so no step re-resolves or builds mid-gate.
out=$(
  {
    uv sync --locked --all-extras --quiet &&
      uv run --no-build --no-sync ruff check darkhunt_telemetry tests &&
      uv run --no-build --no-sync ruff format --check darkhunt_telemetry tests &&
      uv run --no-build --no-sync mypy &&
      uv run --no-build --no-sync bandit -c pyproject.toml -r darkhunt_telemetry &&
      uv run --no-build --no-sync pytest -q
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
