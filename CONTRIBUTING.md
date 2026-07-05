# Contributing to sqbyl

Thanks for your interest. sqbyl is a git-native, plain-files toolkit with a few
strong architectural invariants — respecting them is what keeps the project's
accuracy claims defensible and its runtime shippable. This guide gets you set up
and points at the rules that matter.

## Development setup

sqbyl uses [`uv`](https://github.com/astral-sh/uv) for environment and dependency
management. From the repo root:

```bash
uv sync          # installs both workspace packages (editable) + dev tooling
```

Run a command in the environment with `uv run <cmd>`.

## The gate (run before every PR)

CI runs exactly these; run them locally first:

```bash
uv run ruff check .            # lint
uv run ruff format --check .   # format
uv run mypy                    # strict type-check
uv run lint-imports            # architectural import boundaries (see below)
uv run pytest                  # tests
```

A single test: `uv run pytest tests/test_x.py::test_name`.

## Architecture: the invariants a PR must respect

These are cross-cutting and expensive to retrofit. CI enforces the structural ones;
reviewers watch the rest.

1. **Two packages, one dependency arrow.** `sqbyl-runtime` is the minimal, shippable
   runtime (release `load()` + `ask()` + logging). `sqbyl` is the full dev toolkit
   (introspect, profile, synth, eval, Coach, judges, console, optimizer, release
   builder). `sqbyl` may depend on `sqbyl-runtime`, **never the reverse** — no dev
   machinery is importable from the runtime. Enforced by `lint-imports`. Decide which
   package your code belongs in.
2. **pydantic v2 is the only schema authority.** Every project-file and release shape
   is a pydantic model. No hand-written validation, no hand-maintained JSON Schema; the
   published release interface is generated from the models.
3. **Dev/test separation is a code boundary.** `synth` writes only `benchmarks/dev.yaml`;
   `coach`/`optimize` read only `dev.yaml`; `benchmarks/test.yaml` is touched by nothing
   but `eval` and humans. The held-out set is reachable only through the sanctioned
   `eval` door — importing it from the dev loop fails CI. Optimizing and measuring on the
   same set is training on the test set.
4. **Mock-first / record-replay; CI never spends tokens.** The `LLMClient` seam has
   real / mock / record-replay implementations. Every LLM-touching path ships with
   mock-based unit tests and at least one record-replay fixture. Do not put an API key
   in a test or fixture.
5. **Cost is estimated-before / metered-during / capped-throughout.** Every paid command
   prints an up-front estimate, shows a live spend meter, meters to `.sqbyl/usage.db`,
   and honors `--budget` (required under `--auto`).
6. **Read-only by default.** Refuse non-`SELECT` at the SQL layer; warn if the credential
   can write. The agent and Coach never issue DDL/DML.
7. **OTel GenAI semantic conventions** for every trace, local-first under `.sqbyl/` but
   exportable.

The design rationale lives in [`docs/sqbyl-design-spec.md`](docs/sqbyl-design-spec.md), and
the build history in [`docs/sqbyl-implementation-plan.md`](docs/sqbyl-implementation-plan.md).

## Pull requests

- Branch off `main`; keep each PR to one coherent change.
- Make sure the full gate is green and add tests for new behavior (a record-replay
  fixture for any new LLM path).
- Write commit messages that explain the *why*. Reference the relevant spec section or
  invariant when it clarifies intent.
- New dependencies land in the right place: heavyweight or dialect-specific ones are
  **optional extras**, lazily imported, so `sqbyl-runtime` stays light.

## Reporting bugs and vulnerabilities

Functional bugs: open an issue with a minimal reproduction (the seeded DuckDB fixture is
a good basis). Security issues: **do not** open a public issue — see
[`SECURITY.md`](SECURITY.md).

## Code of conduct

Participation is governed by our [Code of Conduct](CODE_OF_CONDUCT.md).
