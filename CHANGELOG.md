# Changelog

All notable changes to sqbyl are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once past `1.0`.

Both packages — `sqbyl` (dev toolkit) and `sqbyl-runtime` (shippable runtime) — are
versioned and released **in lockstep**: a given `sqbyl` release pins the exact
`sqbyl-runtime` it was built and tested against. This is distinct from the release
artifact's `schema_version`, which versions the on-disk release JSON interface.

## [Unreleased]

## [0.1.1] — 2026-07-06

A bug-fix release. 0.1.0 shipped with `sqbyl ask` broken against current default
models; upgrade is recommended for anyone on `pip install sqbyl`.

### Fixed

- **Live Claude `ask()` restored** — newer Claude models (e.g. `claude-opus-4-8`)
  reject a custom `temperature`. The client now detects the parameter-rejection error,
  strips the offending parameter, and retries — so the shipped default model works out
  of the box. (#39)
- **`--budget` parsing** — a space-separated `--budget N` value leaked through as a
  positional argument (e.g. mistaken for the project directory); it's now consumed
  correctly across every paid command. (#40)
- **`sqbyl synth` crash on structured output** — models intermittently return a
  forced-tool argument with a nested list field encoded as a JSON *string*;
  `LLMResponse.parse()` now decodes and re-validates once instead of raising. (#42)

### Changed

- PyPI package metadata: Documentation / Changelog / Discussions project URLs and
  discovery keywords, which surface on this release. (#38)

## [0.1.0] — 2026-07-06

The first versioned release. Everything in the implementation plan (Phases 0–9) is
built, tested, and merged. Pre-`1.0`: the CLI surface and project/release file shapes
may still change with minor-version bumps until `1.0`.

### Added

- **Engine** — read-only DB connection layer with a privilege check, schema
  introspection, and $0 column profiling (DuckDB + Postgres first-class; SQLite,
  MySQL, Snowflake, BigQuery behind a dialect seam).
- **Agent runtime** (`sqbyl-runtime`) — the generate → static-validate → execute →
  self-repair → respond pipeline, `load()` a release + `ask()`, and OTel-GenAI traces.
- **Context selection** — include-all / lexical / LLM / LLM-lexical strategies for
  larger schemas.
- **Eval harness** — deterministic result-set scorers first, advisory LLM judges
  second; run reports and run diffs; a sealed held-out `test.yaml` guarded as a
  code boundary.
- **Synthesizer** — execution-grounded candidate questions into the dev set.
- **Review console** — a local web UI for building the golden set and reviewing
  judge verdicts and Coach proposals.
- **The Coach** — reads eval failures and proposes ranked, applyable file diffs at
  the right layer of the examples > semantics > prose hierarchy.
- **Orchestrator + attention router** — parallel fan-out with a leverage-sorted
  review queue and a live spend meter.
- **Cost machinery** — estimate-before / meter-during / cap-throughout, with
  `--budget` and a guided vs. `--auto` posture; guided `sqbyl init`.
- **Release + Optimizer** — `sqbyl release create` emits a single portable,
  `schema_version`'d JSON; `sqbyl optimize` runs the autonomous coach→apply→eval
  loop on dev only.
- **Surface & scale** — `sqbyl serve` / `run <release>`, export adapters (plain
  callable, LangChain tool, stdlib MCP server), and importers (dbt / query logs /
  views → proposed examples + joins).
- **Providers** — provider-neutral behind the `LLMClient` seam: Anthropic **or**
  OpenAI, chosen per project via `model.provider` and used for every role (no mixing).
  Each SDK is an optional extra (`[anthropic]` / `[openai]`).
- **Packaging** — `py.typed` markers so downstream type-checkers see the packages'
  types; PyPI metadata (keywords, classifiers, project URLs); a Trusted-Publishing
  release workflow.

### Notes

- CI never spends API tokens: every LLM path is exercised via the mock / record-replay
  seam. Dependency vulnerabilities are scanned with `pip-audit`; updates arrive via
  Dependabot.

[Unreleased]: https://github.com/jackwerner/sqbyl/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/jackwerner/sqbyl/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/jackwerner/sqbyl/releases/tag/v0.1.0
