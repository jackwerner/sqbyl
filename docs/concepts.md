# Concepts

## The loop

sqbyl reproduces one loop as plain, git-tracked files:

**build → evaluate → get told how to improve → re-evaluate.**

1. **Build.** `sqbyl init` connects read-only, introspects the schema, profiles every column
   with `$0` SQL, infers join candidates, then (after a confirmed estimate) annotates tables
   and columns and synthesizes a benchmark.
2. **Evaluate.** `sqbyl eval dev` runs the agent over your iteration set and scores it —
   deterministically first, advisory LLM judges second.
3. **Get told how to improve.** `sqbyl coach` reads the eval failures and proposes *ranked,
   applyable file diffs* — at the right layer of the **examples > semantics > prose**
   hierarchy. `sqbyl coach apply N` writes them; git tracks every change.
4. **Re-evaluate.** Re-run `eval dev` to see the effect, then `eval test` for the honest,
   held-out number. `sqbyl optimize` automates the coach→apply→eval inner loop on dev.

Everything the agent does is written to an OpenTelemetry-shaped trace the Coach and
synthesizer later learn from.

## Defensible measurement

A natural-language-to-SQL surface is only as good as the accuracy number you can put in
front of stakeholders and stand behind. sqbyl is designed end-to-end to keep that number
honest.

- **Deterministic-first measurement.** The headline accuracy is *result-set correctness* —
  execute the gold SQL and the generated SQL, compare the rows. No LLM sits inside the
  number, so it's reproducible and can't drift with a prompt. LLM judges are strictly
  **advisory**: they triage the ambiguous pile and explain *why* a row is suspect, but they
  never move the reported accuracy. Only a human override is authoritative.

- **Real train/test discipline.** See [dev/test discipline](#devtest-discipline) below.

- **Goodhart-resistance by construction.** The Coach optimizes context against the dev set —
  but it *structurally cannot* move the deterministic accuracy number, it's steered away
  from memorizing benchmark answers (fix the semantics, not the prompt), and it warns you
  that dev gains are **unvalidated until a held-out re-score**.

- **Calibrated, honest uncertainty.** A small eval set is noise-prone, so accuracy carries a
  **Wilson confidence interval** — a 1–2 question flip on 30 questions isn't dressed up as a
  trend. A live **judge↔human agreement** score tells you how far to trust the judge, and
  it's labeled as *selection-biased* rather than overclaimed. The model's own self-reported
  confidence is labeled **"unverified"** — never presented as calibrated.

- **Reproducibility and provenance.** Every scored run is stamped with the **model version
  per role** and the calibration state that shaped it. The release scorecard records the
  exact models the number was earned on, and the runtime warns on model or schema mismatch
  at load.

- **Human-in-the-loop, everywhere.** One pattern runs through the judge, the benchmark
  synthesis, and the Coach: **the LLM proposes, the human disposes, and the correction
  improves the system.**

## Dev/test discipline

`benchmarks/test.yaml` is a **sealed held-out set**. The dev loop — synth, coach, optimizer
— can never read it; that's enforced as a *code boundary* (an import-linter rule in CI), not
a convention you have to remember. Even judge calibration is split-scoped, so dev feedback
can't leak into the test judge.

The headline number is **always the held-out one**, with the dev score shown beside it so
the gap is visible. Optimizing and measuring on the same set is training on the test set;
sqbyl makes that mistake hard to commit.

## The context hierarchy: examples > semantics > prose

The agent's accuracy ceiling is set by metadata and examples; free-text instructions are the
last resort. Both the context compiler and the Coach bake in this hierarchy — the Coach
prefers a column description, synonym, measure, or example over reaching for prose. When you
read a coach proposal, this is why it edits a semantics YAML rather than padding the prompt.

## Cost posture

Free deterministic work runs first at **$0** (connect, profile, infer joins). Paid work is
**estimated before, metered during, and capped throughout**: every paid command prints an
up-front estimate, shows a live spend meter, meters to `.sqbyl/usage.db`, and accepts
`--budget`. In `--auto` mode `--budget` is required and enforced as a hard stop. The
economics of the agent are as legible as its accuracy.

## Architecture: two packages, one dependency arrow

sqbyl ships as **two packages**, so what you develop with is not what you deploy:

- **`sqbyl-runtime`** — the minimal, dependency-light runtime you embed in production: load a
  release, `ask()`, structured logging. No web stack, no eval machinery.
- **`sqbyl`** — the full dev toolkit: introspect, profile, annotate, synth, the eval harness,
  the Coach, LLM judges, the review console, the optimizer, and the release builder.

`sqbyl` depends on `sqbyl-runtime`, **never the reverse** — a one-way boundary enforced in CI
(import-linter). None of the dev/eval machinery can leak into what runs in your app. You
iterate with the toolkit; you ship the runtime. Both are strict-typed (`py.typed`) and
pydantic-backed, and the release interface is a documented, `schema_version`'d JSON that a
third party can read without sqbyl at all.
