# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

This repo is **fully built**: the implementation plan (`docs/sqbyl-implementation-plan.md`, Phases 0–9) is complete, followed by post-implementation enhancements (packaging, CI/supply-chain, enterprise-readiness). It is not yet published to PyPI. Authoritative references:

- `docs/sqbyl-design-spec.md` — the full product design (*what* and *why*). Section references below (e.g. §4) point here.
- `docs/sqbyl-implementation-plan.md` — the phased build sequence; now a record of *how it was built* (all phases complete), not a to-do list.
- `docs/sqbyl-user-journey.md` — a narrated end-to-end run, useful for CLI/UX intent.
- `README.md` — the user-facing entry point.

New work should respect the invariants below (CI enforces the structural ones) and land on the correct side of the two-package boundary. Match the surrounding code's conventions.

## What sqbyl is

An open-source, Claude-powered toolkit for building, evaluating, and iterating on text-to-SQL agents over a user's own SQL database. One Anthropic API key powers everything: the SQL-writing agent, the LLM judges that score it, and the **Coach** that reads eval failures and proposes applyable file diffs. A project is a git-native directory of plain YAML/Markdown; a release is a single portable JSON. The differentiator is the closed improvement loop (build → eval → coach → re-eval), all transparent and editable.

## Toolchain

- **`uv`** for env + dependency management. `uv sync` to install, `uv run <cmd>` to execute.
- **`ruff`** for lint + format: `uv run ruff check .` and `uv run ruff format --check .`
- **`pytest`**: `uv run pytest`. Single test: `uv run pytest path/to/test_x.py::test_name`.
- **`mypy`** in strict mode: `uv run mypy`.
- **import-linter**: `uv run lint-imports` (enforces the package + dev/test boundaries — invariants 1 and 3).
- **pydantic v2** as the schema backbone (see invariants).

The local gate to run before every PR: `ruff check` → `ruff format --check` → `mypy` → `lint-imports` → `pytest`. CI runs that plus a dependency vulnerability audit (`pip-audit`), a license-compat check, and a live-Postgres job — and **must never spend API tokens** (invariant 4).

## Architecture: two packages, one dependency arrow

This is the most important structural fact. There are **two packages**:

- **`sqbyl-runtime`** — the minimal, dependency-light "ship it" runtime. Contains *only* release `load()` + `ask()` + structured logging. This is what gets embedded in a user's production app.
- **`sqbyl`** — the full dev toolkit (introspect, profile, annotate, synth, eval harness, Coach, judges, review console, orchestrator, optimizer, release builder).

`sqbyl` depends on `sqbyl-runtime`, **never the reverse.** None of the dev machinery (eval/synth/coach/judges/console) may be importable from `sqbyl-runtime`. This boundary is enforced by an import-linter rule in CI — if you add code, make sure it lands on the correct side of this line.

## Core data flow

```
Your DB ──introspect/profile──> semantics/*.yaml (with $0 profile: blocks)
                                       │
project files ──context compiler──> prompt ──agent runtime──> {plan, sql, rows, ...}
                                       │                            │
                                  eval harness <───────────────────┘
                                  (deterministic scorers, then LLM judges)
                                       │
                                  Coach reads failures ──> ranked applyable file diffs
```

Everything the agent does is written to a trace log (OTel-shaped) that the Coach and synthesizer later learn from. The agent runtime pipeline (generate → static-validate → execute → self-repair → respond) lives in `sqbyl-runtime`; everything that *improves* the agent lives in `sqbyl`.

## Non-negotiable invariants

These are cross-cutting and **expensive to retrofit** — uphold them in every change, not as a later cleanup pass:

1. **Package boundary.** `sqbyl-runtime` stays minimal and never imports dev machinery. Every new module: decide which package it belongs in. Import-linter is the backstop.
2. **pydantic is the only schema authority.** Every project-file and release-artifact shape is a pydantic v2 model. No hand-written validation, no hand-maintained JSON Schema. The published release interface (`schema_version`'d JSON) is **generated** from the models; a test fails if the checked-in schema drifts.
3. **Dev/test separation is a code boundary, not a convention.** `synth` writes only `benchmarks/dev.yaml`; `coach`/`optimize` read only `dev.yaml`; `benchmarks/test.yaml` is touched by nothing but `eval` and humans. Code paths for coach/synth/optimize must not even *receive* `test.yaml`. Tests assert this. (Optimizing and measuring on the same set is training on the test set — the whole loop edits context to push a score up.)
4. **Mock-first / record-replay; CI never spends tokens.** The `LLMClient` seam has three impls: real, mock (scripted deterministic), and record-replay. Every LLM-touching code path ships with mock-based unit tests and at least one record-replay fixture.
5. **Cost is estimated-before / metered-during / capped-throughout.** Every paid command prints an up-front estimate, shows a live spend meter, meters to `.sqbyl/usage.db`, and accepts `--budget` (guided: pause-and-ask; `--auto`: hard-stop, and `--budget` is *required* in `--auto`). Route paid commands through the estimator from the day they're written, even before the full Phase 7 machinery exists.
6. **Read-only by default.** Refuse non-SELECT at the SQL layer; on connect, inspect the credential's privileges and warn (with a suggested fix) if it can write. Never let the agent or Coach issue DDL/DML.
7. **OTel GenAI semantic conventions** for every trace from the first one written. Traces stay local-first (`.sqbyl/`) but must be exportable to any OTel backend.

## Design principles that shape implementation choices

When a design decision is ambiguous, prefer the option that better satisfies these (from spec §1.5):

- **Proactive, never a surprise charge.** Free deterministic work (connect/introspect/profile/joins) runs first at $0; paid work is pre-planned with a confirmed estimate.
- **Examples > semantics > prose.** The agent's accuracy ceiling is set by metadata and examples; text instructions are the last resort. The context compiler *and* the Coach bake in this hierarchy — the Coach should avoid reaching for prose when a column description, synonym, measure, or example would fix it.
- **Route attention.** Confidence on every machine decision; auto-apply high-confidence (with one-click undo); surface only the ambiguous/business-meaning items, sorted by leverage.
- **Small-space posture.** Default toward ≤5–7 tables; warn beyond that. "Include everything" in the context compiler is fine until ~30 tables (large-schema LLM/lexical selection is a late phase — don't build it early).

## Testing assets

A checked-in **DuckDB fixture** (the `orders`/`customers` schema from spec §4) plus a complete dogfood sqbyl project serve as both the README example and the end-to-end CI smoke test. New features should be exercisable against this fixture under record-replay with zero external dependencies. Postgres also has a live-server CI job (`tests/test_postgres_integration.py`), skipped locally unless `SQBYL_TEST_POSTGRES_URL` is set.

## First-class dialects

DuckDB + Postgres are the first-class dialects, behind a thin dialect seam; SQLite, MySQL, Snowflake, and BigQuery are also supported behind it (added last). Don't couple core logic to a single dialect's quirks.
