<!-- Thanks for contributing! Keep PRs to one coherent change. -->

## What & why

<!-- What does this change and why? Link the spec section / invariant / issue if relevant. -->

## Checklist

- [ ] `uv run ruff check .` and `uv run ruff format --check .` pass
- [ ] `uv run mypy` passes (strict)
- [ ] `uv run lint-imports` passes (package + dev/test boundaries)
- [ ] `uv run pytest` passes; new behavior has tests
- [ ] Any new LLM-touching path has a mock test **and** a record-replay fixture (no API keys in tests)
- [ ] Code landed in the correct package (`sqbyl-runtime` stays minimal; dev machinery lives in `sqbyl`)
- [ ] New heavyweight/dialect deps are optional extras, lazily imported

See [`CONTRIBUTING.md`](../CONTRIBUTING.md) for the invariants behind these.
