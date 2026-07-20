<!--
Thanks for the PR! Briefly describe what changes and why.
For non-trivial changes, link the issue you discussed first.
-->

## Summary

<!-- One paragraph: what does this PR do? -->

## Why

<!-- One paragraph: what problem does it solve, or what does it enable? Link issues if any. -->

Closes #<!-- issue number, or delete this line -->

## Type of change

- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds capability)
- [ ] Breaking change (fix or feature that would change existing behavior)
- [ ] New validator / masking change
- [ ] Documentation only
- [ ] Refactor / internal cleanup

## Checklist

- [ ] I ran the full gate locally and everything passes:
      `uv run ruff check darkhunt_telemetry tests && uv run ruff format --check darkhunt_telemetry tests && uv run mypy && uv run bandit -c pyproject.toml -r darkhunt_telemetry && uv run pytest -q`
- [ ] I added tests covering the change (regression test for bug fixes; positive + negative cases for new features/validators)
- [ ] If I touched masking, I kept parity with the TS SDK (attribute keys unchanged; `rules.json` synced from upstream, not hand-edited here)
- [ ] If I changed public API, I updated the README accordingly
- [ ] I did not bump the version by hand (CI owns `MAJOR.MINOR.<run_number>`; only bump `MAJOR.MINOR` in `_version.py` for a new series)

## Test evidence

<!--
For non-trivial changes, paste relevant test output OR explain how you verified end-to-end. Examples:
- "Added tests/test_masking.py cases X and Y, all pass"
- "Emitted a span with input={'email': 'user@example.com'} and verified it arrives at the backend masked as [EMAIL]"
Delete this section for trivial doc/lint changes.
-->

## Risks / things reviewers should look at

<!--
Anything you want a reviewer's eyes on specifically. E.g. "the regex change in
rules.json affects rule ordering — please double-check the parity test output."
-->
