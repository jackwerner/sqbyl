# Getting started

## Install

sqbyl is provider-neutral — the provider SDKs are optional extras, so pick the one you'll
use and install just that.

```bash
pip install 'sqbyl[anthropic]'          # full dev toolkit, Claude backend
pip install 'sqbyl[openai]'             # full dev toolkit, OpenAI backend
pip install 'sqbyl-runtime[anthropic]'  # the lightweight "ship it" runtime only
```

A bare `pip install sqbyl` installs the toolkit without a provider SDK; the first LLM call
then errors with the extra to install. Installing both `[anthropic]` and `[openai]` is fine
— sqbyl imports whichever your project's `provider` names, at call time.

!!! note "Requirements"
    - **Python 3.11+**
    - An LLM provider API key — Anthropic (`ANTHROPIC_API_KEY`) or OpenAI (`OPENAI_API_KEY`)
    - A SQL database reachable **read-only** (`DATABASE_URL`). DuckDB and Postgres are the
      first-class dialects; SQLite, MySQL, Snowflake, and BigQuery are supported behind a
      dialect seam.

Developing on sqbyl itself? See
[`CONTRIBUTING.md`](https://github.com/jackwerner/sqbyl/blob/main/CONTRIBUTING.md) for the
from-source setup with [`uv`](https://github.com/astral-sh/uv).

## Quickstart

Point sqbyl at a database and a key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export DATABASE_URL=postgresql://readonly_user@warehouse.internal/analytics   # read-only role
```

Then run the guided setup. There's **no config file to hand-write** — if `sqbyl.yaml` is
missing, `init` walks you through creating it (name, dialect, connection URL, provider,
API-key env var). It then does the free, deterministic work first (connect, read schema,
profile every column with read-only SQL), checks your provider key works (a `$0`, token-free
call), shows you a **costed plan**, and only spends after you confirm:

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

You land in a review queue — not a blank page — surfacing only the decisions a human has to
make (e.g. *"what counts as an active customer?"*), each with a sensible default pre-filled.
Accept your way to the readiness target, then run the loop:

```bash
sqbyl eval dev        # measure against your iteration set
sqbyl coach           # ranked, applyable file diffs for whatever still fails
sqbyl coach apply 1 2 # writes the edits (git tracks them)
sqbyl eval test       # the honest, held-out number
sqbyl release create --tag v1
```

`release create` emits one portable JSON — the agent's "brain" (semantics, instructions,
examples, judge prompts, scorecard). The model, key, and database are **not** baked in;
they're injected wherever it runs.

For the full narrative, read the [user journey](sqbyl-user-journey.md). To put the release
behind your own API, see [Embedding the runtime](guides/embedding.md).

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

The dev/test split is load-bearing — see [dev/test discipline](concepts.md#devtest-discipline).
