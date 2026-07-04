# sqbyl

**An open-source, Claude-powered toolkit for building, evaluating, and iterating on text-to-SQL agents over your own database.**

Bring your own database. Bring one Anthropic API key. sqbyl uses Claude to both *answer* natural-language questions against your data **and** *coach you* on how to make the agent answer them better — then ships the result as a single portable file you can drop into production.

> **Status: pre-release / not yet on PyPI.** The full toolkit (Phases 0–9) is built and tested; install is from source for now, and command/file shapes may still change before a first tagged release. See [Project status](#project-status) for the capability map. The design is fully specified in [`sqbyl-design-spec.md`](sqbyl-design-spec.md), with a first-run walkthrough in [`sqbyl-user-journey.md`](sqbyl-user-journey.md) and the build sequence in [`sqbyl-implementation-plan.md`](sqbyl-implementation-plan.md).

---

## Why sqbyl

If you want a trustworthy natural-language-to-SQL surface over a plain Postgres/DuckDB/Snowflake warehouse, your options are roughly: pay for a closed platform that locks the semantic layer, the judges, and the optimizer inside a walled garden — or wire up a library yourself and hand-author all the metadata, evals, and prompt tuning.

sqbyl is the middle path. It reproduces the **build → evaluate → get told how to improve → re-evaluate** loop as plain files in a git repo — and it's built so the accuracy number that loop produces is one you can actually **report to stakeholders and defend**:

- **No black box.** Every prompt, judge, and improvement proposal is readable, editable plain text/JSON.
- **No second vendor.** One Anthropic key powers the agent, the judges, and the Coach. Context selection is LLM/lexical, so there's no embeddings provider or vector store to run.
- **No surprise bill.** The free, deterministic work (connect, profile, infer joins) runs first at $0. Paid work is estimated up front, metered live, and capped by `--budget`.
- **Versioned like code.** Your whole "agent" is a directory of YAML you diff, review, and `git revert`.
- **Defensible by design.** The headline accuracy is deterministic and measured on a *held-out* set the improvement loop can never touch — so "we hit 94%" is a claim that survives scrutiny, not a benchmark you overfit. ([more below](#built-for-defensible-ml-systems))

---

## Built for defensible ML systems

A natural-language-to-SQL surface is only as good as the accuracy number you can put in front of stakeholders and stand behind. sqbyl is designed end-to-end around the ML-systems principles that keep that number honest — the same discipline you'd want before deploying any evaluated agent at scale:

- **Deterministic-first measurement.** The headline accuracy is *result-set correctness* — execute the gold SQL and the generated SQL, compare the rows. No LLM sits inside the number, so it's reproducible and can't drift with a prompt. LLM judges are strictly **advisory**: they triage the ambiguous pile and explain *why* a row is suspect, but they never move the reported accuracy. Only a human override is authoritative.

- **Real train/test discipline.** `benchmarks/test.yaml` is a **sealed held-out set**. The dev loop — synth, coach, optimizer — can never read it; that's enforced as a *code boundary* (an import-linter rule in CI), not a convention you have to remember. Even judge calibration is split-scoped, so dev feedback can't leak into the test judge. **The headline number is always the held-out one**, with the dev score shown beside it so the gap is visible.

- **Goodhart-resistance by construction.** The Coach optimizes context against the dev set — but it *structurally cannot* move the deterministic accuracy number, it's steered away from memorizing benchmark answers (fix the semantics, not the prompt), and it warns you that dev gains are **unvalidated until a held-out re-score**. Optimizing and measuring on the same set is training on the test set; sqbyl makes that mistake hard to commit.

- **Calibrated, honest uncertainty.** A small eval set is noise-prone, so accuracy carries a **Wilson confidence interval** — a 1–2 question flip on 30 questions isn't dressed up as a trend. A live **judge↔human agreement** score tells you exactly how far to trust the judge, and it's labeled as *selection-biased* rather than overclaimed. The model's own self-reported confidence is labeled **"unverified"** — never presented as calibrated.

- **Reproducibility and provenance.** Every scored run is stamped with the **model version per role** and the calibration state that shaped it. A score is never divorced from what produced it — the release scorecard records the exact models the number was earned on, and the runtime warns on model or schema mismatch at load.

- **Human-in-the-loop, everywhere.** One unifying pattern runs through the judge, the benchmark synthesis, and the Coach: **the LLM proposes, the human disposes, and the correction improves the system.** Every judge verdict, synthesized question, and fix is a reviewable proposal, not a fait accompli.

- **Cost honesty.** Free, deterministic work runs first at **$0** (connect, profile, infer joins). Paid work is estimated before, metered live, and capped by `--budget`. The economics of the agent are as legible as its accuracy.

The short version: **sqbyl helps you ship a text-to-SQL agent whose accuracy you can actually report** — because the number is deterministic, held out, provenance-stamped, and defended against the ways evaluation loops quietly lie to you.

---

## Architecture: two packages, one dependency arrow

sqbyl ships as **two packages**, so what you develop with is not what you deploy:

- **`sqbyl-runtime`** — the minimal, dependency-light runtime you embed in production: load a release, `ask()`, structured logging. No web stack, no eval machinery.
- **`sqbyl`** — the full dev toolkit: introspect, profile, annotate, synth, the eval harness, the Coach, LLM judges, the review console, the optimizer, and the release builder.

`sqbyl` depends on `sqbyl-runtime`, **never the reverse** — a one-way boundary enforced in CI (import-linter). None of the dev/eval machinery can leak into what runs in your app. You iterate with the toolkit; you ship the runtime. Both are strict-typed (`py.typed`) and pydantic-backed, and the release interface is a documented, `schema_version`'d JSON that a third party can read without sqbyl at all.

---

## Who this is for

You're putting a natural-language-to-SQL surface over your own warehouse — an internal analytics tool or a product feature — and you need an accuracy number you can defend, plus a system you can read, edit, and version. You work in git, a SQL database, and YAML. You have (or can provision) a **read-only** database role and an Anthropic API key.

---

## Install

> Not yet published to PyPI. For now, clone and install from source.

```bash
git clone <this-repo-url> sqbyl
cd sqbyl
uv sync                      # sqbyl uses uv for env + dependency management
```

Once published, the intended install will be:

```bash
pip install sqbyl           # full dev toolkit
pip install sqbyl-runtime   # the lightweight "ship it" runtime only
```

Maintainers: [`PUBLISHING.md`](PUBLISHING.md) has the step-by-step for cutting a release.

---

## Quickstart

Point sqbyl at a database and a key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export DATABASE_URL=postgresql://readonly_user@warehouse.internal/analytics   # use a read-only role
```

Then run the guided setup. It does the free, deterministic work first (connect, read schema, profile every column with read-only SQL), shows you a **costed plan**, and only spends after you confirm:

```bash
sqbyl init
```

```
▸ connecting…………………………………… done
▸ reading schema………………………………… 42 tables, 380 columns
▸ profiling columns (read-only SQL)… done   ($0 — no LLM)
▸ heuristic join candidates……………… 11 found, 3 ambiguous

Ready to enrich with Claude. Here's the plan and the estimate:
  annotate 380 columns + 42 tables   ~$1.20
  synthesize ~40-question benchmark  ~$0.60
  baseline eval                      ~$0.30
  ─────────────────────────────────────────
  estimated total                   ~$2.15   on claude-opus-4-8

Proceed? [Y]es · [s]elect steps · [m]odel · [n]o
```

You land in a review queue — not a blank page — surfacing only the decisions a human has to make (e.g. *"what counts as an active customer?"*), each with a sensible default pre-filled. Accept your way to the readiness target, then:

```bash
sqbyl eval dev        # measure against your iteration set
sqbyl coach           # ranked, applyable file diffs for whatever still fails
sqbyl coach apply 1 2 # writes the edits (git tracks them)
sqbyl eval test       # the honest, held-out number
sqbyl release create --tag v1
```

`release create` emits one portable JSON — the agent's "brain" (semantics, instructions, examples, judge prompts, scorecard). The model, key, and database are **not** baked in; they're injected wherever it runs.

For the full narrative, read [`sqbyl-user-journey.md`](sqbyl-user-journey.md).

---

## Shipping a release

Production is "just a model with logs." The dev machinery (eval, synth, coach, console) does **not** come along — you embed the lightweight runtime:

```python
from sqbyl_runtime import load

agent = load("revenue-analytics.v1.json", db=env.DATABASE_URL, model="claude-opus-4-8")

@app.post("/ask")          # your API, your auth, your scaling
def ask(q: str):
    return agent.ask(q)    # → {plan, sql, rows, used_assets, usage, latency}
```

It inherits your app's auth, connection pooling, and observability. `sqbyl run <release>` / `sqbyl serve` exist for non-Python callers and quick HTTP exposure, but are **intentionally not hardened** — don't put `sqbyl serve` on the open internet.

---

## Project layout

A sqbyl project is a git-native directory of plain files:

```
my-project/
├── sqbyl.yaml          # manifest: db connection, model(s), defaults
├── instructions.md     # the (small) global instruction block
├── semantics/          # one YAML per table: columns, profiles, joins, measures, filters
├── examples/           # NL → SQL few-shot pairs
├── trusted/            # vetted, parameterized "source of truth" queries
├── benchmarks/
│   ├── dev.yaml        # iteration set: Coach/Optimizer tune against this
│   └── test.yaml       # held-out set: Coach/Optimizer NEVER see it
└── .sqbyl/             # runs, traces, usage, caches (gitignored)
```

The dev/test split is load-bearing: optimizing and measuring on the same set is training on the test set, so the headline accuracy is always the held-out number. Full format reference in [the design spec, §4](sqbyl-design-spec.md).

---

## Command reference (intended surface)

```
sqbyl init [<db-url>]     # guided: free profile → costed plan → confirm → step through
                          #   (--auto --budget $5 for CI; --dry-run to estimate only)
sqbyl review              # attention queue + golden-set / judge / proposal review (web UI)
sqbyl eval [dev|test]     # run the eval harness → scored report + run diff
sqbyl synth [--n 40]      # execution-grounded candidate questions → dev set
sqbyl coach [apply N...]  # review / apply pre-computed context edits (dev only)
sqbyl optimize --budget $5 --target 0.9   # autonomous coach→apply→eval loop on dev
sqbyl ask "..."           # one-shot NL→SQL→result
sqbyl release create --tag v1             # bless current version → portable JSON
sqbyl cost <command>      # estimate $ / tokens, spend nothing
sqbyl reset [--all]       # clear local .sqbyl/ state (keeps cost history unless --all)
```

Per-step à-la-carte commands (`introspect`, `profile`, `annotate`, `judge`, `runs`, `serve`, `run`) are documented in [the spec, §10](sqbyl-design-spec.md).

---

## Configuration

Project configuration lives in `sqbyl.yaml`; secrets are referenced by `env:` name, not inlined:

```yaml
name: revenue-analytics
database:
  dialect: postgresql        # postgresql | duckdb | snowflake | bigquery | mysql | sqlite
  url: env:DATABASE_URL
  read_only: true            # refuses non-SELECT; warns if the credential can write
model:
  api_key: env:ANTHROPIC_API_KEY
  default: claude-opus-4-8    # per-role models (agent/judge/coach/...) override default
  # base_url: env:CLAUDE_GATEWAY   # optional: route Claude through a proxy / AI gateway
```

See [§4 of the spec](sqbyl-design-spec.md) for the full manifest, including per-role model pinning and automation toggles. To route through a corporate proxy or AI gateway, set `model.base_url` (or pass `base_url=` to the runtime `load()`) — no other change needed.

---

## Security & data handling

The section a security reviewer will look for:

- **Read-only by default.** sqbyl refuses non-`SELECT` at the SQL layer and, on connect, inspects the credential's privileges and warns (with a suggested fix) if it can write. The agent and the Coach never issue DDL/DML. Point it at a dedicated read-only role.
- **Secrets by reference.** Connection strings and API keys are `env:`-indirected — never written to project files, releases, or traces.
- **Your data stays yours.** Query result rows are not persisted to committed project files or traces; imported SQL that carries literal values is flagged for review before it can land. A release is the agent's *brain* — semantics, prompts, examples — never rows.
- **Local-first, exportable telemetry.** Traces follow the OpenTelemetry GenAI conventions and are written under `.sqbyl/`; export them to any OTel backend when you want.
- **CI never spends tokens.** Every LLM path runs against a mock / record-replay seam, so continuous integration never calls the API. Dependencies are vulnerability-scanned (`pip-audit`), license-checked, and updated via Dependabot.
- **Production hardening is yours.** You embed the runtime in your own service, inheriting its auth, TLS, pooling, and rate limiting. `sqbyl serve` is a localhost dev convenience, not a production server — don't expose it.

Governance, RBAC, lineage, and catalog management are deliberately **not** reimplemented — that's your database's job ([non-goals](sqbyl-design-spec.md#non-goals)).

---

## Requirements

- Python (managed via [`uv`](https://github.com/astral-sh/uv))
- An Anthropic API key (`ANTHROPIC_API_KEY`)
- A SQL database, reachable read-only (`DATABASE_URL`). DuckDB and Postgres are the first-class dialects; SQLite, MySQL, Snowflake, and BigQuery are supported behind a dialect seam.

---

## Project status

The full build sequence in [`sqbyl-implementation-plan.md`](sqbyl-implementation-plan.md) (Phases 0–9) is complete: every capability below is built, tested, and merged.

| Capability | State |
|---|---|
| Engine: introspect + profile + agent runtime (`sqbyl ask`) | ✅ built |
| Golden set + eval harness (`synth`, `review`, `eval`) | ✅ built |
| Coach + LLM judges | ✅ built |
| Guided `init`, orchestrator, cost machinery | ✅ built |
| Release + runtime + optimizer | ✅ built |
| More dialects, serve, exports, importers | ✅ built |

**Not yet released to PyPI**, so commands and file shapes may still change before a first tagged version — see [`sqbyl-enhancements.md`](sqbyl-enhancements.md) for the post-implementation backlog (packaging, docs, enterprise-readiness).

---

## Documentation

The spec is the *why*, the journey is a *narrated first run*, and the plan is a record of *how it was built*:

- [`sqbyl-design-spec.md`](sqbyl-design-spec.md) — the full product design specification.
- [`sqbyl-user-journey.md`](sqbyl-user-journey.md) — a narrated first run, start to ship.
- [`sqbyl-implementation-plan.md`](sqbyl-implementation-plan.md) — the phased build sequence (Phases 0–9, complete).
- [`sqbyl-enhancements.md`](sqbyl-enhancements.md) — the post-implementation backlog (packaging, docs, enterprise-readiness).

## License

[MIT](LICENSE) © Jack Werner
