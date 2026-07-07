# Changelog

All notable changes to sqbyl are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once past `1.0`.

Both packages — `sqbyl` (dev toolkit) and `sqbyl-runtime` (shippable runtime) — are
versioned and released **in lockstep**: a given `sqbyl` release pins the exact
`sqbyl-runtime` it was built and tested against. This is distinct from the release
artifact's `schema_version`, which versions the on-disk release JSON interface.

## [Unreleased]

## [0.3.0] — 2026-07-07

### Added

- **Opt-in natural-language answers on `ask()`.** `ask()` still returns the authoritative
  `sql`/`columns`/`rows`; now a chat/assistant surface can also get a plain-English sentence
  without re-implementing the summarization step itself. Enable it at load
  (`load(..., narrate=True)`), per call (`agent.ask(q, narrate=True)`), or on the CLI
  (`sqbyl ask "…" --narrate`) and `result.answer` is populated by **one** final call grounded
  strictly on the executed rows. It's **off by default** so the deterministic, `$0`-by-default
  runtime is unchanged; the call is estimated up front, traced as its own `narrate` GenAI span,
  and metered as a distinct `narrate` role/model (`narration_model=` / `model.narrate_model`).
  The narrated sentence is a convenience over the rows, which remain the source of truth. The
  release JSON interface (`schema_version`) is unchanged — narration is a load-time injection,
  not baked into the portable brain.

## [0.2.1] — 2026-07-07

A follow-up patch to 0.2.0: a missing (not just empty) `benchmarks/test.yaml` was still
handled badly.

### Fixed

- **`sqbyl optimize` no longer crashes at the finish line on a missing held-out set** — the
  single held-out score is the last step, after the whole paid loop; a missing (or empty)
  `benchmarks/test.yaml` raised an uncaught `FileNotFoundError` there and discarded the
  entire run's frontier. It now skips the held-out scoring, keeps the frontier, and the
  report says the held-out wasn't scored (so a rising dev number isn't mistaken for a
  validated one).
- **`sqbyl eval test` on a missing `test.yaml`** prints the split-aware "hand-author the
  held-out set" hint instead of a raw traceback; the underlying "no benchmark file" error is
  now split-aware everywhere (dev → run `sqbyl synth`; test → hand-author it, invariant 3).
  Completes the 0.2.0 fix, which only covered an empty-but-existing file.

## [0.2.0] — 2026-07-07

Fixes and new affordances from a first-time-user setup pass against a live Postgres.
The headline items close two ways the Coach/optimizer could corrupt a project file, and
add a supported path for schema drift. Upgrade recommended.

### Added

- **`sqbyl init` scaffolds a missing `sqbyl.yaml`** — a first run in an empty directory
  now walks you through name / dialect / connection URL / provider / API-key env var (and
  writes a ready-to-fill template in non-interactive / `--auto` mode) instead of raising a
  bare `FileNotFoundError`.
- **`$0` credential preflight** — `init` verifies the LLM key with a token-free provider
  call before you approve any spend, so a bad/expired key fails fast rather than partway
  through paid enrichment. (No-op under record-replay, so CI still spends nothing.)
- **`sqbyl eval show <split> <id>`** — prints one saved row's full detail (plan,
  generated vs gold SQL, each scorer's pass/fail + detail, each judge's verdict +
  rationale) for headless/terminal review, no browser required. `$0`.
- **`sqbyl introspect --sync`** — additively merges new live columns into existing
  semantics files (keeping every description/synonym/profile) and reports dropped columns
  without deleting them — a non-destructive alternative to `--force`, which rewrites the
  whole file. `init` also names schema drift explicitly in its free pass.
- **`sqbyl coach --regenerate`** — `init` now prints the Coach proposals it already paid
  for, and `sqbyl coach` reuses an existing report for the current dev run for `$0`
  (`--regenerate` forces a fresh, paid call).

### Fixed

- **Coach could corrupt a project file** — a proposal whose `find` anchor lived in a
  different file, or whose edit introduced a schema-invalid field (e.g. `description_note`
  on a column, `description` on a join), could be written to disk and break the next
  command. Proposals are now validated/repaired at generation (mislocated anchors are
  relocated; schema-breaking edits are stripped), `coach apply` re-validates the target
  against its pydantic schema before writing, and an edit-less proposal refuses to apply
  instead of reporting a false success.
- **`sqbyl optimize` could leave an un-reverted file on a crash** — the per-trial snapshot
  restore now runs in a `finally`, so any exception after an edit lands rolls the file back
  before propagating. `optimize` also warns when the project isn't a git repo, since its
  "revert with `git checkout`" guidance assumes one.
- **`init --model` now reprices every role** — a cheaper-model swap moved only the
  annotate/eval lines while synth/judge stayed pinned to the default; the override now
  applies to every role (synth, judge, coach, eval) unless a role is explicitly pinned in
  `sqbyl.yaml`, so the estimate matches what actually gets spent.
- **`sqbyl review` returned a clean error instead of a 500** — `/accept` and `/rerun` now
  translate a failed database connection (env unset, DB down, rotated credential) into the
  typed `db_error` result the UI already understands.
- **`sqbyl eval test` on an empty held-out set** no longer suggests `sqbyl synth` (which
  cannot write `test.yaml` — invariant 3); it now points you to hand-author it.

### Changed

- The judge Review tab shows a "pile complete" banner and dims the just-resolved card once
  every row is reviewed, so finishing the last row reads as done rather than ignored.

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

[Unreleased]: https://github.com/jackwerner/sqbyl/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/jackwerner/sqbyl/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/jackwerner/sqbyl/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/jackwerner/sqbyl/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/jackwerner/sqbyl/releases/tag/v0.1.0
