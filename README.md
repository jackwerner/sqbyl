# sqbyl

**An open-source, Claude-powered toolkit for building, evaluating, and iterating on text-to-SQL agents over your own database.**

Bring your own database. Bring one Anthropic API key. sqbyl uses Claude to both *answer* natural-language questions against your data **and** *coach you* on how to make the agent answer them better — then ships the result as a single portable file you can drop into production.

> **Status: pre-release / under active development.** This README describes the intended workflow. Some commands and packages below are not built yet — see [Project status](#project-status) for what works today. The design is fully specified in [`sqbyl-design-spec.md`](sqbyl-design-spec.md), with a first-run walkthrough in [`sqbyl-user-journey.md`](sqbyl-user-journey.md) and the build sequence in [`sqbyl-implementation-plan.md`](sqbyl-implementation-plan.md).

---

## Why sqbyl

If you want a trustworthy natural-language-to-SQL surface over a plain Postgres/DuckDB/Snowflake warehouse, your options are roughly: pay for a closed platform that locks the semantic layer, the judges, and the optimizer inside a walled garden — or wire up a library yourself and hand-author all the metadata, evals, and prompt tuning.

sqbyl is the middle path. It reproduces the **build → evaluate → get told how to improve → re-evaluate** loop as plain files in a git repo:

- **No black box.** Every prompt, judge, and improvement proposal is readable, editable plain text/JSON.
- **No second vendor.** One Anthropic key powers the agent, the judges, and the Coach. Context selection is LLM/lexical, so there's no embeddings provider or vector store to run.
- **No surprise bill.** The free, deterministic work (connect, profile, infer joins) runs first at $0. Paid work is estimated up front, metered live, and capped by `--budget`.
- **Versioned like code.** Your whole "agent" is a directory of YAML you diff, review, and `git revert`.

sqbyl deliberately does **not** reimplement governance, RBAC, or catalog management — that's your database's job. See [non-goals](sqbyl-design-spec.md#non-goals).

---

## Who this is for

You want to clone this and develop your own text-to-SQL pipeline against your data. You're comfortable with a CLI, a SQL database, and editing YAML. You have (or can make) a **read-only** database role and an Anthropic API key.

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
```

Per-step à-la-carte commands (`introspect`, `profile`, `annotate`, `judge`, `runs`, `serve`, `run`) are documented in [the spec, §10](sqbyl-design-spec.md).

---

## Configuration

Everything lives in `sqbyl.yaml`. Credentials never do — use `env:` indirection:

```yaml
name: revenue-analytics
database:
  dialect: postgresql        # postgresql | duckdb | snowflake | bigquery | mysql | sqlite
  url: env:DATABASE_URL
  read_only: true            # refuses non-SELECT; warns if the credential can write
model:
  api_key: env:ANTHROPIC_API_KEY
  default: claude-opus-4-8    # per-role models (agent/judge/coach/...) override default
```

See [§4 of the spec](sqbyl-design-spec.md) for the full manifest, including per-role model pinning and automation toggles.

---

## Requirements

- Python (managed via [`uv`](https://github.com/astral-sh/uv))
- An Anthropic API key (`ANTHROPIC_API_KEY`)
- A SQL database, reachable read-only (`DATABASE_URL`). DuckDB and Postgres are the first-class dialects; others land later.

---

## Project status

This is being built milestone by milestone. Roughly:

| Capability | State |
|---|---|
| Engine: introspect + profile + agent runtime (`sqbyl ask`) | 🔜 in progress |
| Golden set + eval harness (`synth`, `review`, `eval`) | 🔜 planned |
| Coach + LLM judges | 🔜 planned |
| Guided `init`, orchestrator, cost machinery | 🔜 planned |
| Release + runtime + optimizer | 🔜 planned |
| More dialects, serve, exports | 🔜 later |

The authoritative, ordered build sequence is in [`sqbyl-implementation-plan.md`](sqbyl-implementation-plan.md). Expect commands and file shapes to change until a first tagged release.

---

## Documentation

- [`sqbyl-design-spec.md`](sqbyl-design-spec.md) — the full product design specification.
- [`sqbyl-user-journey.md`](sqbyl-user-journey.md) — a narrated first run, start to ship.
- [`sqbyl-implementation-plan.md`](sqbyl-implementation-plan.md) — the phased technical build plan.

## License

_TODO: choose and add a license (the project is intended to be open source)._
