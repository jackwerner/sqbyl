# Changelog

All notable changes to sqbyl are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once past `1.0`.

Both packages — `sqbyl` (dev toolkit) and `sqbyl-runtime` (shippable runtime) — are
versioned and released **in lockstep**: a given `sqbyl` release pins the exact
`sqbyl-runtime` it was built and tested against. This is distinct from the release
artifact's `schema_version`, which versions the on-disk release JSON interface.

## [Unreleased]

## [0.5.0] — 2026-07-16

Brings `annotate` up to the honesty bar the rest of sqbyl already holds: it reconciles every
signal, never silently overwrites your truth, and routes what it can't ground to the human —
"the LLM proposes, the human disposes," now applied to the build step (findings B11/B12).

### Changed

- **`annotate` never overwrites an authoritative description.** A description carried in from
  the database catalog (a column/table `COMMENT`, which `introspect` already captures) or
  hand-edited is treated as ground truth: the annotator is now *shown* it and told to reconcile
  with it, and the merge is **fill-only** — a non-empty description is never replaced, only blank
  slots are filled. Synonyms are unioned onto existing ones, not replaced. (Previously the draft
  blindly overwrote both — the `district.A4` "labeled *area* when it's *population*, catalog
  comment discarded" case.)
- **`annotate` withholds what it can't ground, and routes it to review.** An un-described column
  the annotator isn't confident about (below `defaults.auto_apply_threshold`) — **or one whose
  type disagrees with its content** (a numbers-stored-as-text column, see below) — is no longer
  written as a confident sentence. It's held back and surfaced as a pre-filled proposal in the
  `sqbyl review` console's leverage-sorted queue, to accept/edit/reject. Only confident,
  previously un-described, well-typed columns auto-fill. Withholds are proportional to columns
  that genuinely can't be grounded (they don't flood a wide schema); `annotate` prints how many
  were held for review; the parallel `init` wave follows the same rules. Contested synonyms (the
  collision cap) now naturally route here too.

### Added

- **Numbers-stored-as-text detection (B12).** The profiler flags a text-declared column whose
  values are (almost) all numeric — common in dumped/dynamically-typed schemas — as
  `profile.numeric_text`, and captures the values' **numeric min/max** (text columns otherwise
  get no range). The magnitude is what disambiguates the meaning — a `district` column ranging to
  ~1.2M is *population*, not *area in km²* — so it's rendered into the annotator's prompt and the
  review card, not just a bare "it's a number" flag. Because the declared type is actively
  misleading here, such a column is always routed to review even when the model is confident (the
  `district.A4` "confidently labeled *area*" case, which a confidence gate alone would miss).
  (Additive optional field; the release schema is regenerated, backward-compatible.)

## [0.4.5] — 2026-07-09

Closes the last of the Sonnet structured-output parse sites: the agent's answer path is now
model-portable to a flagship model out of the box, matching the annotate (B9) and eval (B4)
guards already in place.

### Fixed

- **The agent's answer path no longer crashes on a generation that omits `sql`, and one bad
  generation can't abort an eval run (finding B10).** `claude-sonnet-5` sometimes returns a
  generation with `plan`/`used_assets` but no `sql` (folding the query into the plan or emitting
  a clarification); the unguarded `response.parse(AgentGeneration)` let that `ValidationError`
  propagate out of `ask()` → `score_run()` and lose every other question. The parse is now
  guarded: a malformed generation becomes a failed attempt that feeds self-repair (with a prompt
  naming the missing `sql` field), and if every attempt fails the question gets a clean error
  verdict instead of an exception. `AgentGeneration` also unwraps a one-level nested wrapper
  (mirrors the annotator's B9 fix), so a wrapped-but-complete answer parses without a repair
  round. This is the same "one bad output nukes the batch" fix as the 0.4.2 eval guard (B4) and
  the 0.4.4 annotate guard (B9), applied at the generation-parse step.

## [0.4.4] — 2026-07-09

More BIRD/Spider hardening from the benchmark re-run: the agent now handles messy
identifiers, `annotate` survives a stronger model's output shape, and the wide-table
collision noise is quieted.

### Fixed

- **`annotate` no longer crashes on `claude-sonnet-5`'s output shape, and one bad table
  can't abort the batch (finding B9).** Sonnet intermittently returns a table annotation
  wrapped in a nested object (`{"table_description": {…}}`) instead of the flat fields the
  schema declares; `TableAnnotation` now unwraps that one level (a pydantic `before`
  validator) rather than raising. Independently, the per-table loop is now guarded — a table
  that still fails to parse is skipped with a warning and the run continues, keeping the
  tables already annotated instead of losing the whole batch (the same failure shape as the
  0.4.2 eval fix).

### Changed

- **The agent is shown messy identifiers pre-quoted (finding B5).** Real-world column and
  table names with spaces or special characters (BIRD's `Charter School (Y/N)`,
  `Enrollment (K-12)`) now reach the agent already quoted for the project's dialect, with a
  one-line reminder to reproduce them exactly — so it copies the correct token instead of
  emitting unparseable unquoted SQL. Applied per identifier: clean names render exactly as
  before, so existing prompts (and their cassettes) are byte-for-byte unchanged.
- **Synonym-collision detection no longer floods on wide real-world tables (finding B6).**
  A word shared across three or more columns is now treated as generic table vocabulary and
  dropped, rather than raising a warning for every column pair that happens to share it. On
  schemas with triplicated families like BIRD `california_schools`'s `AdmFName1/2/3` /
  `AdmLName1/2/3` / `AdmEmail1/2/3` — where every column answers to "administrator"/"name" —
  this collapses dozens of unactionable warnings to (at most) the genuine two-way contests,
  and stops the cap from needlessly demoting half the table's confidence. A token contested by
  exactly two columns (the `cost_price` vs `unit_price` "price" case) is unchanged.

## [0.4.3] — 2026-07-09

Two profiler crashes surfaced once 0.4.2 let `profile` actually run on the BIRD/Spider
SQLite databases — both on exactly the "dirty" real-world schemas those benchmarks are
built from, and both aborting the whole `profile` command on the affected table.

### Fixed

- **The profiler now quotes every identifier it emits.** `profile` interpolated raw column
  and table names straight into SQL, so it crashed the moment it met a real-world name with a
  space or parenthesis — e.g. BIRD `california_schools`'s `Charter School (Y/N)` or
  `Enrollment (K-12)`. The read-only guard's parser rejected the unquoted SQL
  (`UnparseableSqlError`) and, because that guard runs before execution, the whole `profile`
  command aborted and *no* column on the table got profiled — losing profile grounding on
  exactly the messy schemas where it matters most. The profiler now quotes column and table
  names per-dialect (`sqlglot`, each part of a `schema.name` independently) everywhere it
  builds SQL: the row count, the stats query, sampling, top-k, and the Python quantile pull.
- **The Python percentile path tolerates non-numeric junk in a "numeric" column.** SQLite and
  MySQL are dynamically typed, so a column classified numeric can still hold `''` (empty string)
  for a missing value. The Python quantile helper (SQLite/MySQL) cast every value with `float()`
  and crashed on `''` — aborting `profile` on the table, e.g. Spider `wta_1`. Non-numeric values
  are now dropped before the cast, matching how the in-SQL percentile path (DuckDB/Postgres)
  coerces or ignores them.

## [0.4.2] — 2026-07-09

Makes SQLite first-class for the dev toolkit and hardens the eval loop — findings from
pointing sqbyl at the BIRD and Spider benchmarks, whose databases ship as SQLite.

### Fixed

- **`introspect` now works on SQLite.** `discover_tables` queried `information_schema.tables`,
  which SQLite doesn't have, so the whole dev pipeline was unreachable at step one on a declared
  dialect. It now falls back to the SQLAlchemy inspector for SQLite (the same seam introspection
  already uses for columns and keys); Postgres/DuckDB/MySQL keep the existing path.
- **`profile` now works on SQLite — and correctly per-dialect.** The profiler branched only
  DuckDB-vs-else-Postgres, so every other dialect silently got Postgres SQL and SQLite's
  `percentile_cont … WITHIN GROUP` failed to parse — disabling the profile-grounded value hints
  that are one of the agent's biggest accuracy levers. Reworked into a real per-dialect strategy:
  dialects with an in-SQL percentile aggregate (DuckDB/Postgres/Snowflake/BigQuery) embed it;
  those without (SQLite/MySQL) compute quantiles in Python from the column's values. Sampling
  clauses are per-dialect too. (SQLite is tested; MySQL/Snowflake/BigQuery use documented syntax
  but are not exercised against a live server.)
- **One unparseable model generation no longer aborts a whole eval run.** The pipeline's
  static-validation step caught only static-validation and write errors, so an `UnparseableSqlError`
  (e.g. the unquoted spaced identifiers common in real-world schemas) propagated out of `ask()`
  and lost every other question's result. It's now caught and becomes a wrong answer that feeds
  self-repair, like any other bad generation.
- **`sqbyl --version` (and `sqbyl_runtime.__version__`) now report the real version.** Both
  packages' `__version__` was a hardcoded `"0.0.0"` placeholder, so `--version` printed `0.0.0`
  regardless of what was installed (since 0.4.0). It now resolves from installed package metadata
  (`importlib.metadata`), falling back to `0.0.0` only when run from a source tree with no metadata.

### Internal

- CI now exercises SQLite for the toolkit (`test_sqlite_toolkit.py`), closing the coverage gap
  that let the SQLite introspect/profile breakage ship — CI previously ran only DuckDB and Postgres.

## [0.4.1] — 2026-07-08

### Fixed

- **Synonym-collision detection is no longer drowned in topical noise.** The `$0` collision pass
  (added in 0.4.0) flagged every column pair that shared the table's own entity root — an orders
  table's `order_id`/`order_date` both "about" orders — turning a 6-table schema into ~38 warnings
  and burying the one that matters (`price` → `cost_price`/`unit_price`). The detector now excludes
  the table's own name (de-pluralized) as topical and treats `identifier` as the generic ID word it
  is. On a representative schema this cut 11 collisions to 2, keeping the real contest and dropping
  the noise — so the warning stays usable on the 30+ table schemas that need it most.
- **`CoachProposal.is_prose` now tracks the target file, not the model's self-reported layer.** The
  Coach sometimes mislabels a well-targeted structured edit (a real `semantics/*.yaml` column
  change) as `layer=instruction`. That stamped it with the "⚠ global prose — last resort" flag a
  reviewer is trained to skip *and* force-routed it to human review. `is_prose` is now derived from
  whether `target_file` is `instructions.md`, so a structured edit is judged by where it actually
  writes. (Removed the now-unused `PROSE_LAYERS` export; added `PROSE_FILE`.)

## [0.4.0] — 2026-07-08

### Added

- **`columns_superset` benchmark scoring.** A benchmark question can now set
  `match_mode: columns_superset` so a result that reproduces every gold column and row but adds
  *extra* informative columns scores **correct** instead of landing in `manual_review`. Default
  stays `exact` (the honest, strict bar). This also unblocks the optimizer, which credits fixes
  off the deterministic `correct` set — a superset answer the Coach improves now counts. The
  never-read `eval_note` field was removed (it looked load-bearing but did nothing).
- **`sqbyl eval --trials N`.** Re-runs the eval N times and reports the accuracy spread, making
  hosted-model inference variance (real even at temperature 0) visible so a single number isn't
  mistaken for a ship/no-ship call. A single-trial run now also prints a one-line variance
  caveat. Only the representative (median) run is persisted; every pass meters its real spend.
- **`sqbyl optimize --trials N` / `--require-significant`.** `--trials` scores each candidate
  edit N times and keeps it only when a **majority** of trials clear the gain bar (a variance
  guard against ratcheting a noisy re-run into the frontier); `--require-significant` additionally
  gates keeps on the paired sign test.
- **`sqbyl coach --from-test-failure <id>`.** A sanctioned, guardrailed path from a held-out
  test failure to a reviewed fix: it diagnoses one failure from the agent's **own trace** (its
  gold is walled off — the diagnoser's input type has no gold field, and the module is in the
  import-linter contract that forbids reaching `eval.heldout`), proposes a **general** context
  edit for human review (never `--auto`, never auto-applied), stamps the proposal's held-out
  provenance, and **quarantines** the item so its next `eval test` score is flagged as no longer
  an independent measurement.
- **Synonym-collision detection in `annotate`.** After the per-table draft, a `$0`, deterministic
  pass flags synonyms that could equally describe a sibling column (the classic `cost` on
  `cost_price` vs `unit_price`), caps the contested columns' confidence below the auto-apply
  threshold, and surfaces a `⚠` line in `annotate` and `init` — so a contested term isn't
  silently applied behind a clean-looking synonyms list.

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
