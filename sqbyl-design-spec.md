# sqbyl — Product Design Specification

*An open-source, Claude-powered toolkit for building, evaluating, and iterating on text-to-SQL agents over your own data.*

> **Codename:** "sqbyl" (from SQL + 'sibyl'... an oracle you consult about your data). 
> **Status:** Design spec / v0. **Audience:** the engineer building this.
> **One-line pitch:** Databricks Genie + Agent Bricks, unbundled from Databricks — bring your own database, bring one Anthropic API key, and let Claude both *answer* the questions and *coach you* on how to make the agent answer them better.

---

## 1. Why this exists

Databricks ships two tightly-coupled capabilities that, together, are unusually good at turning a messy warehouse into a trustworthy natural-language analytics surface:

- **Genie Spaces** — a curated, domain-specific NL→SQL chat surface. You point it at a focused set of tables, annotate them, give it example SQL, define business semantics (metrics/filters), add "trusted assets" (vetted queries that are the single source of truth), and a small amount of text instruction. It generates SQL with chain-of-thought reasoning, executes it, and returns results/visualizations. You curate it iteratively against a **benchmark** set of question→gold-SQL pairs and a monitoring feed.
- **Agent Bricks** — describe the task in natural language, connect data, and it *auto-generates task-specific evals and LLM judges, synthesizes domain-relevant test data, and searches optimization techniques* (prompt optimization, fine-tuning, RL) to hand you a quality/cost-tradeoff frontier you pick from.

The combination is powerful precisely because it closes the loop: **build → evaluate → let the system tell you how to improve → re-evaluate.** Crucially, the platform itself recommends instruction edits and prompt tweaks.

The catch: it is **black-boxed behind Databricks** — Unity Catalog, SQL warehouses, partner-powered AI features, account-level entitlements. The semantic layer, the judges, the optimizer, and the model are all inside the walled garden. You cannot run it against a plain Postgres/Snowflake/DuckDB instance with your own model key, you cannot read the judge prompts, and you cannot version the whole thing as plain files in a git repo.

**sqbyl unbundles this.** It is a Python library + CLI that reproduces the *loop*, not the lock-in:

1. You **define your data** (any SQL database) and a portable semantic layer in plain files.
2. You **define agents** (text-to-SQL, optionally multi-step).
3. Everything hooks to a single **Anthropic API key**. The *same* Claude model that powers the agent also powers the judges and — the headline feature — **the Coach**, an LLM that reads your eval failures and proposes concrete, applyable edits to your instructions, examples, synonyms, and joins.

### Non-goals
- Not a Databricks clone. No Unity Catalog, no warehouse management, no governance/RBAC layer (that's the host DB's job).
- Not a BI dashboarding tool. Visualization is a thin convenience, not the product.
- Not a fine-tuning platform in v1. Optimization happens in **context space** (instructions/examples/selection), which is where most real-world accuracy lives and which works with a hosted API key. Weight-tuning is a later, optional plug-in.

---

## 1.5 The wedge and the five push principles

**We will not out-govern Databricks, and we won't try.** Unity Catalog, lineage, RBAC, warehouse management — that is their moat, and we cede it *deliberately*. BYO-DB is not a limitation we apologize for; it is the strategic move that makes everything else possible: the host database already does governance, security, and access control, so we don't reimplement any of it. Every hour not spent rebuilding a catalog is an hour spent removing a click.

That leaves exactly two axes where an incumbent platform is weakest and where we choose to win:

- **User-friendliness.** The shortest possible path from "I have a database" to "I have a shippable, benchmarked agent." Measured in messages and clicks, and we drive both toward zero.
- **Open-source transparency.** Every prompt, every judge, every Coach proposal, and the release format itself are readable plain text/JSON and editable. No black box. This is simultaneously a trust win for users and an onboarding win for contributors to a public repo.

Everything in this spec is subordinate to five **push principles**. When a design choice is ambiguous, the one that better satisfies these wins:

1. **Proactive, not reactive — but never a surprise charge.** The tool does the *preparation* before you ask. All the free, deterministic work (connect, introspect, profile, candidate joins, lexical value sampling) runs up front with no prompting and no cost. The *paid* work (annotation, synthesis, baseline eval, fix pre-compute) is pre-planned with a cost estimate and stepped through with smart defaults you confirm — never fired silently. You arrive to results, not a blank prompt; you also never type `init` and watch \$50 of credits vanish without seeing it coming. Proactive means "Claude already drafted the answer and shows you the bill," not "Claude already spent the money."
2. **Parallel by default.** Independent work runs concurrently (per-table annotation, join inference, candidate synthesis, baseline eval). You wait once, not N times.
3. **Route attention; spend it only where it's scarce.** Everything carries a confidence score. High-confidence work is applied and shown as *already done* (one-click undo); only the ambiguous, conflicting, or business-meaning-dependent items are surfaced. The human looks at what *only* a human can decide — e.g. "what counts as an *active* customer?" — not at what the data already answers.
4. **Never a problem without a proposed fix.** Nothing is surfaced as an open question. Every item is a decision with a sensible default already filled in — **accept / edit / reject** — including the items that need human knowledge (best guess pre-filled). Decisions, not homework.
5. **Fewest round-trips, full visibility.** Batch reviews, "accept all high-confidence," keyboard-driven, smart defaults everywhere, and a visible **readiness signal** showing distance to shippable. Round-trips that cost money carry a **live spend meter and an up-front estimate**, so "low mental calories" never means "low awareness of cost." Minimizing total messages and clicks is a hard product metric, tracked like accuracy — and so is never spending unannounced.

These are why someone picks sqbyl over wiring up Vanna themselves *and* over Databricks for a team that doesn't need the governance tax.

---

## 2. What we replicate, and how it maps

| Databricks concept | sqbyl equivalent | Notes |
|---|---|---|
| Genie Space | **Project** (a directory of files) | The unit of curation. Versioned in git. |
| Unity Catalog tables + column comments | **Semantic layer** (`semantics/*.yaml`) introspected from any DB + human/LLM annotations | We can't rely on a pre-existing rich catalog (Vanna's well-known pain) — so sqbyl introspects schema and helps you author the metadata. |
| SQL expressions (metrics/filters) | **Measures & named filters** | Business semantics defined once, reused. Addresses "what does *revenue* mean" consistency. |
| Example SQL queries | **Examples** (`examples/*.yaml`) — NL→SQL pairs | Retrieved as few-shot context; not necessarily executed. |
| Trusted assets (vetted queries/UC functions) | **Trusted assets** — named, parameterized, executable SQL/templates | Single source of truth; agent prefers these and cites them. |
| Text instructions (≤1 per space) | **Instructions** (`instructions.md`) | Kept deliberately small; sqbyl enforces the "examples over prose" hierarchy. |
| Auto-suggested queries from query history | **Importers** | Optional: ingest a query log / dbt models / existing views and propose examples + joins. |
| Prompt matching (value/spelling correction) | **Value matching** | Optional lexical lookup over high-cardinality categorical columns (declared sample values + `SELECT DISTINCT`), no embeddings. |
| Benchmarks (≤500 Q, gold SQL, run as fresh convos) | **Eval sets** (`benchmarks/*.yaml`) | Each question = NL + optional gold SQL (or gold function). Run statelessly. |
| Genie benchmark scoring (result-set match + LLM judge + "manual review needed") | **Eval harness** — layered scorers | See §7. |
| Agent Bricks auto-eval + synthetic data + optimizer | **Synthesizer** + **Coach** + **Optimizer** | See §6 and §8. The differentiator. |
| Monitoring feed + 👍/👎 ratings | **Trace log** + feedback capture | Local JSONL/SQLite; feeds the Coach. |
| Mosaic AI LLM judges | **Claude judges** with *open, versioned* prompts | You can read and edit every judge prompt. |
| Genie API / publishing a space for use elsewhere | **Release artifact + `sqbyl-runtime` + exports (LangChain/MCP)** | Ship the curated brain; inject model + DB at the destination. |
| (curation lives in the Databricks UI) | **`sqbyl review` local console** | Build/maintain the golden set over plain files, locally. |

**Design principle inherited from Genie:** *data quality and examples beat text instructions.* The agent's accuracy ceiling is set by metadata and examples; prose instructions are the last resort. sqbyl bakes this hierarchy into both the context compiler and the Coach's recommendations.

**Design principle inherited from Genie curation:** *start small, iterate.* A new project defaults to a "small space" posture — warns if you exceed ~5–7 tables or stack conflicting instructions, and the Coach prefers minimal, targeted edits.

---

## 3. System architecture

```
                          ┌────────────────────────────────────────────┐
                          │                  Anthropic API              │
                          │   (single ANTHROPIC_API_KEY, one model id)  │
                          └───┬───────────────┬───────────────┬─────────┘
                 agent calls  │      judge     │     coach     │  synth
                              ▼                ▼               ▼
┌──────────┐   introspect  ┌─────────────────────────────────────────────┐
│  Your DB │◄──────────────│            sqbyl core (Python)              │
│ (pg/duck/│   execute SQL │                                             │
│  sf/...) │◄──────────────│  ┌───────────┐  ┌───────────┐  ┌──────────┐ │
└──────────┘               │  │ Context   │  │ Agent     │  │  Eval    │ │
                           │  │ compiler  │─▶│ runtime   │─▶│ harness  │ │
   ┌───────────────┐       │  └───────────┘  └───────────┘  └────┬─────┘ │
   │ Project files │◄─────▶│        ▲                            │       │
   │ (git repo):   │  read │        │        ┌───────────┐       │       │
   │  semantics/   │  write│        └────────│   Coach   │◄──────┘       │
   │  examples/    │       │                 │(LLM tutor)│ failures+traces│
   │  instructions │       │                 └─────┬─────┘               │
   │  benchmarks/  │       │   proposes file diffs  │                    │
   │  trusted/     │◄──────┼────────────────────────┘                    │
   │  .sqbyl/      │ traces│                                             │
   └───────────────┘       └─────────────────────────────────────────────┘
```

### Components
1. **Schema introspector + profiler** — connects via SQLAlchemy (or DB-native drivers), reads tables, columns, types, PK/FK, and existing comments, and runs a cheap, read-only **column profile** in the same pass: per column, the deterministic stats a human would otherwise eyeball — null fraction, distinct count (→ categorical vs continuous), min/max/range for numerics and dates, a few percentiles, and top-k frequent values for low-cardinality columns. All of it is plain aggregate SQL (sampled via `TABLESAMPLE` + the read-only row caps on large tables, never a full billion-row scan), so it costs **zero tokens** and happens before any LLM call. The profile is what lets the annotator do the grunt work: Claude drafts descriptions, synonyms, and `sample_values` *grounded in the actual data* (it can see that `amount_cents` ranges 0–4.2M with no nulls and infer the cents unit, that `status` has 3 distinct values, that `created_at` spans 2019→today) instead of guessing from names. Emits draft `semantics/*.yaml` with a `profile:` block per column. For databases without FKs, proposes candidate joins (name/type heuristics + LLM) for human confirmation.
2. **Context compiler** — turns the project files + the live question into the actual prompt context: selected tables, their annotated DDL, relevant measures/filters, applicable few-shot examples, applicable trusted assets, and instructions. Handles **context selection** (which subset of a large schema/example bank to include — done by LLM shortlisting against a compact table/example catalog, or lexical match; no embeddings/vector store) and **prompt caching** of the stable schema block.
3. **Agent runtime** — the query pipeline (§5). Generates SQL with chain-of-thought, validates, executes, self-repairs, optionally summarizes/plots.
4. **Eval harness** — runs an eval set as fresh stateless conversations and scores each with the layered scorers (§7). Produces a run report + accuracy/cost/latency aggregates, stored and diffable across runs.
5. **Coach** — *the differentiator* (§8). Consumes failing/edge traces and emits ranked, concrete, applyable edits to the project files, each with rationale and predicted impact.
6. **Synthesizer** — cold-start helper. From schema + a seed description, Claude proposes candidate benchmark questions with gold SQL (you accept/reject), mirroring Agent Bricks' synthetic data.
7. **Registry / state** (`.sqbyl/`) — local SQLite + JSONL: traces, run history, model/usage accounting, content hashes of each project version so runs link to the exact config that produced them. Traces are structured to the **OpenTelemetry GenAI semantic conventions** so they stay local-first but can be exported to Langfuse/Phoenix/any OTel backend a team already runs — observability without a bespoke format lock-in.
8. **Orchestrator** — the engine behind the push principles (§1.5, §5.5). After the free deterministic pass and the user's costed go-ahead, it fans the **approved** paid work out **in parallel** (per-table annotation, join inference, synthesis, baseline eval, fix pre-computation) within the confirmed budget, tolerates partial failure (a failed unit becomes a low-confidence card, never a hard stop), and reports a single live progress checklist with a running spend meter. Concurrency is **bounded and rate-limit-aware**: a worker pool sized to the account's API tier, with retry-and-backoff on 429s, so a 42-table fan-out doesn't self-DoS the key. It also lands the first call that fills the prompt cache before releasing the parallel wave, so the rest read from cache instead of each paying the cache-write cost.
9. **Attention router + readiness scorer** — assigns a confidence to every machine-made decision, **auto-applies** high-confidence ones (with one-click undo), and surfaces only the rest into a single review queue **sorted by leverage** (the fewest decisions that move readiness the most). Computes the live readiness signal ("86% accuracy · 4 decisions to reach 95%").

---

## 4. The project format (plain files, git-native)

A sqbyl project is a directory. Everything is human-readable and diffable — the entire Genie "space" config becomes code.

**Two design rules govern this format:**

- **Dev/test separation is structural, not optional.** Benchmarks are split into a **dev** set (synth-fed; what the Coach and Optimizer iterate against) and a **held-out test** set (ideally hand-authored; the Coach and Optimizer are *forbidden* from reading it). Because sqbyl's whole loop edits context to push a score up, optimizing and measuring against the same set would be training on the test set — so the headline accuracy that justifies a release (§11) is always reported on the held-out set, with the dev number shown alongside for transparency. The gap between them *is* your overfitting signal.
- **Every file format is backed by a pydantic v2 model.** The YAML/JSON shapes below are not hand-validated — they deserialize into pydantic models that own validation, (de)serialization, and **auto-generated JSON Schema**. That generated schema *is* the "documented, versioned public interface" the release artifact promises (§11), so the working YAML and the shipped JSON share one source of truth instead of drifting.


```
my-project/
├── sqbyl.yaml                 # project manifest: db connection, model, defaults
├── instructions.md            # the (small) global instruction block
├── semantics/
│   ├── orders.yaml            # one file per table/view
│   └── customers.yaml
├── examples/
│   └── revenue.yaml           # NL → SQL few-shot examples
├── trusted/
│   └── mrr.sql                # vetted, parameterized "single source of truth" queries
├── benchmarks/
│   ├── dev.yaml               # iteration set: synth-fed; Coach/Optimizer tune against this
│   └── test.yaml              # held-out set: ideally human-authored; Coach/Optimizer NEVER see it
└── .sqbyl/                    # runs, traces, usage, caches (gitignored)
```

### `sqbyl.yaml`
```yaml
name: revenue-analytics
description: >
  Answers revenue, churn, and pipeline questions for the GTM team.
database:
  dialect: postgresql            # postgresql | duckdb | snowflake | bigquery | mysql | sqlite
  url: env:DATABASE_URL          # never hard-code creds; env: indirection
  read_only: true                # sqbyl refuses non-SELECT unless explicitly allowed.
                                 # On connect, sqbyl checks the role's privileges and WARNS if
                                 # the credential can write — strongly suggest pointing
                                 # DATABASE_URL at a dedicated read-only role.
model:
  provider: anthropic
  # One key, many roles. Each role's model is independently pinnable; unset roles
  # fall back to `default`. Swap any of these without touching the others.
  api_key: env:ANTHROPIC_API_KEY
  default: claude-opus-4-8
  agent_model: claude-opus-4-8       # the engine that writes SQL
  selection_model: claude-opus-4-8   # large-schema table/example shortlisting
  orchestrator_model: claude-opus-4-8 # cold-start fan-out (annotate/infer/synth)
  synth_model: claude-opus-4-8       # benchmark candidate generation
  coach_model: claude-opus-4-8       # failure→fix proposals
  judge_model: claude-opus-4-8       # can pin a different/independent model for judge independence
automation:
  # Whether the loop runs itself after an eval, or waits to be asked.
  auto_judge: true               # run LLM judges automatically on ambiguous rows
  auto_coach: true               # pre-compute fix proposals the moment a run finishes
  # If either is false, sqbyl still surfaces a one-line nudge after each run
  # ("3 rows need judging · run `sqbyl judge`" / "fixes available · run `sqbyl coach`")
  # so the capability is discoverable without being forced on.
defaults:
  max_tables_warn: 7             # "small space" nudge inherited from Genie best practice
  self_repair_attempts: 2
  prompt_caching: true
```

### `semantics/orders.yaml`
```yaml
table: analytics.orders
description: One row per confirmed order. Excludes carts and draft orders.
synonyms: [purchases, transactions, sales]
columns:
  - name: order_id
    type: bigint
    description: Primary key.
    profile: { nulls: 0.0, distinct: 4216890, min: 1, max: 4216890 }   # auto, deterministic, $0
  - name: customer_id
    type: bigint
    description: FK to customers.customer_id.
    profile: { nulls: 0.0, distinct: 318204 }
  - name: amount_cents
    type: bigint
    description: Order total in cents. Divide by 100 for dollars.
    profile: { nulls: 0.0, min: 0, max: 4200000, p50: 4999, p95: 28900 }   # range → "this is cents"
  - name: status
    type: text
    description: One of 'confirmed','refunded','partial_refund'.
    profile: { nulls: 0.0, distinct: 3 }
    sample_values: [confirmed, refunded, partial_refund]   # top-k from profile; powers value-matching
  - name: created_at
    type: timestamptz
    description: Order confirmation time (UTC).
    profile: { nulls: 0.0, min: "2019-02-01", max: "2026-06-29" }   # data's real coverage window
# profile: blocks are written by the introspector at connect time (read-only, sampled on big
# tables) and are what let Claude draft the descriptions/synonyms/sample_values above. Drop a
# column's raw sample_values (or set `profile: false`) to keep PII out of the project files.
joins:
  - to: analytics.customers
    type: many_to_one
    on: "orders.customer_id = customers.customer_id"
measures:
  - name: net_revenue
    description: Revenue net of refunds, in dollars.
    sql: "SUM(CASE WHEN status='confirmed' THEN amount_cents ELSE 0 END)/100.0"
filters:
  - name: last_quarter
    sql: "created_at >= date_trunc('quarter', now()) - interval '3 months'"
```

### `examples/revenue.yaml`
```yaml
- question: What was net revenue last month?
  sql: |
    SELECT SUM(amount_cents)/100.0 AS net_revenue
    FROM analytics.orders
    WHERE status='confirmed'
      AND created_at >= date_trunc('month', now()) - interval '1 month'
      AND created_at <  date_trunc('month', now());
  tags: [revenue, time-window]
```

### `trusted/mrr.sql`
```sql
-- @name: monthly_recurring_revenue
-- @params: month (date)
-- @description: Official MRR definition. Prefer this over ad-hoc revenue math.
SELECT ...;
```

### `benchmarks/dev.yaml`  (the held-out `test.yaml` has the same shape)
```yaml
- id: q_rev_lastmo
  question: How much revenue did we book last month?
  gold_sql: |
    SELECT SUM(amount_cents)/100.0 FROM analytics.orders
    WHERE status='confirmed' AND created_at >= date_trunc('month', now()) - interval '1 month'
      AND created_at < date_trunc('month', now());
  # gold can instead be: gold_asset: monthly_recurring_revenue
  eval_note: "Must net out refunds; a single scalar in dollars is expected."
```

Same schema for both sets; the only difference is who may read them. `synth` writes to `dev.yaml`; `coach`/`optimize` read `dev.yaml`; `test.yaml` is touched by nothing but `eval` and humans. (A new project may start with a single set; the split becomes load-bearing the moment you run the Optimizer.)

---

## 5. Agent runtime — the query pipeline

A single `ask()` is a stateless pipeline (multi-turn is a thread of these with prior turns added to context, exactly like a Genie conversation thread):

1. **Resolve & select.** Pick the relevant subset of tables, examples, measures, filters, and trusted assets. For small projects, include everything. For large schemas, **Claude shortlists** the relevant tables/examples from a compact catalog (names + one-line descriptions), optionally narrowed by lexical match — this is context selection, not vector retrieval, so it stays on the single Anthropic key. Optionally value-match high-cardinality terms ("EMEA" → `region='emea'`) via a lexical lookup against declared sample values.
2. **Compile context.** Build the prompt: cached schema/semantics block + selected examples + applicable trusted assets + instructions + the question. Trusted assets are presented as *preferred* building blocks.
3. **Generate.** Claude produces (a) a short chain-of-thought plan and (b) candidate SQL. Optionally sample N candidates and self-rank (inference-time scaling helps weaker setups; cheap to gate behind a flag).
4. **Static-validate.** `EXPLAIN`/parse the SQL against the live schema (no execution). Catch nonexistent columns, type errors, dialect issues.
5. **Execute** (read-only enforced) on the user's DB.
6. **Self-repair.** On static or execution error, feed the error back to Claude up to `self_repair_attempts` times. (Mirrors the "submit feedback and regenerate" affordance in Genie.)
7. **Respond.** Return `{plan, sql, rows, used_assets, usage, latency}`. Optionally an NL summary and a chart spec. If the agent used a trusted asset, the response cites it (Genie's "this came from a trusted asset" transparency).

Everything is written to the trace log for the Coach to learn from later.

---

## 5.5 The push experience — `sqbyl init` to a shippable agent

This section is the product. Everything else is parts; this is how they feel.

**One command, but it walks you through it.** `sqbyl init postgres://…` (or run it bare and answer one prompt for the connection string) is the entire required input — but `init` is **guided by default**: it does the free work, shows you a costed plan, and steps you through confirmations, rather than fanning out and spending on its own. The model is `sam deploy --guided`, not a black-box batch job: high visibility, low mental calories, no surprise bill.

**Phase 1 — the free pass (no tokens, runs immediately).** Connect, introspect, and **profile** every column (§3.1) — all deterministic, read-only SQL. You see what you've got before anything is spent:

```
sqbyl init  ▸ connecting…………………………………… done
            ▸ reading schema………………………………… 42 tables, 380 columns
            ▸ profiling columns (read-only SQL)… done   ($0 — no LLM)
            ▸ heuristic join candidates……………… 11 found, 3 ambiguous
            ──────────────────────────────────────────────
            Ready to enrich with Claude. Here's the plan and the estimate:

              annotate 380 columns + 42 tables        ~$1.20
              resolve 3 ambiguous joins               ~$0.05
              synthesize ~40-question dev benchmark    ~$0.60
              baseline eval (40 Qs)                    ~$0.30
              ────────────────────────────────────────────
              estimated total                         ~$2.15   on claude-opus-4-8

            Proceed? [Y]es · [s]elect steps · [m]odel (cheaper) · [n]o
```

Nothing paid has happened yet. You can drop steps, swap to a cheaper model for the bulk work, or bail — and the number you approve is a cap, with a **live spend meter** ticking against it as work runs (it pauses and re-asks before exceeding what you approved).

**Phase 2 — the stepped enrichment (after you confirm).** Approved phases run — efficiently, still parallel under the hood — but each surfaces its results as you go, SAM-style: a default already filled in, three suggestions where there's a real choice, and a one-key confirm. Annotation, **execution-grounded** synthesis (§6.A — only questions whose gold SQL actually ran survive), the baseline eval, and (if `auto_coach` is on) the Coach's fix proposals land as you watch the meter, not after a silent gap. A unit that fails (e.g. the LLM can't confidently describe one cryptic column) doesn't block anything — it becomes a low-confidence card.

**You arrive to a review queue, not a blank page.** The attention router has already applied everything it's confident about and surfaced only what needs you, **sorted by leverage** — the smallest set of decisions that moves readiness the most. Every card is a *decision with a default*, never an open question:

```
sqbyl review                                        Agent: 86% ▸ 96% in 6 decisions

①  Business meaning needed — best guess filled in
    "active customer" → customers.is_active = true              [Accept] [Edit] [Reject]
    (used by 4 benchmark questions)

②  Accept measure  net_revenue                      fixes 3 benchmark Qs  ▸ +8%
    SUM(CASE WHEN status='confirmed' THEN amount_cents ELSE 0 END)/100      [Accept] [Edit]

③  Override judge on Q14?  judge said WRONG — but result rows match gold     [Confirm] [Override]

④  Low-confidence join:  orders ⋈ shipments on order_id                    [Accept] [Edit] [Reject]

      ⌨  a accept · e edit · r reject · A accept-all-high-confidence · ⏎ next
```

**The readiness meter is the finish line.** It always shows where you are and how far to target ("86% · 6 decisions to 96%"), and it updates live as you accept. The loop is intentionally short: accept the queue → readiness clears the target → **`sqbyl release create`** is the one remaining click, and you have the portable JSON (§11). Most projects should reach shippable in a single review pass — minutes, a handful of keystrokes.

**Unattended mode for CI.** `sqbyl init --auto --budget $5` skips the prompts and runs the whole thing headless — but `--budget` is **required** in `--auto` (it hard-stops at the cap rather than pausing to ask), so even the non-interactive path can't silently overspend. Every step is also its own command (`profile`, `annotate`, `synth`, `eval`, `coach`) for power users, and re-running `init`/`eval` on a changed schema re-orchestrates only what changed. The guided push is the default because the default should be the fast path *and* the visible one.

---

## 6. Closing the loop — three modes of improvement

This is the part that makes sqbyl more than "Vanna with Claude." There are three escalating ways to improve a project, all powered by the same API key.

**A. Synthesizer — execution-grounded (cold start).** `sqbyl synth` drafts candidate benchmark questions, but the key move is that it does **not** just emit question→SQL pairs and hope. For every candidate it writes gold SQL, **executes it against your real database, and discards anything that errors or returns empty/degenerate results** — so a human only ever reviews questions whose answer already runs. It seeds from your semantic layer (each measure, join, and named filter becomes question fodder) so candidates exercise real business logic; stratifies by difficulty (single-table aggregate → filtered → joined → multi-step); and generates phrasing *variants* per canonical question (Genie benchmarks test phrasing variation explicitly). The best source of all is real questions — traces from `sqbyl ask`/`serve` and any imported query log get promoted into candidates. Survivors flow straight into the review console (§6.5) and, once accepted, land in the **dev** set — never the held-out `test.yaml`, which stays human-curated so it can keep the synth/coach/optimize loop honest. This is what makes a golden set buildable in an afternoon instead of by hand-writing 50 questions, and it mirrors Agent Bricks' synthetic-data generation — except every accepted item is one you've seen execute.

**B. Coach (human-in-the-loop, the default).** Proposals are **pre-computed automatically** the moment an eval run finishes — you don't ask for coaching, it's already waiting in the review queue as accept/edit/reject cards (§8). `sqbyl coach` just re-opens them.

**C. Optimizer (autonomous, opt-in).** `sqbyl optimize --budget $5 --target 0.9` — runs Coach→apply→re-eval in a loop **against the dev set**, keeping edits that improve the dev score and reverting those that don't, until the target accuracy or the cost/iteration budget is hit. Returns a **frontier** of project versions with their accuracy/cost/latency, and you pick one — directly analogous to Agent Bricks handing you optimization iterations to choose from, except every version is a readable git diff. Crucially, the optimizer never sees `test.yaml`; the held-out score is computed **once** on the version you pick, and a large dev↔test gap is surfaced as an overfitting warning rather than hidden.

---

## 6.5 The review console — building the golden set without leaving the platform

`sqbyl review` launches a local web app (FastAPI + a small bundled UI; no cloud, no account). It is a thin, opinionated surface over the project files — **not a second source of truth.** Everything it writes lands back in `benchmarks/*.yaml`, `examples/*.yaml`, and the semantic files, so the golden set stays plain, git-diffable files.

It is also the home of the **attention queue** (§5.5): the console never shows you everything, only the leverage-sorted set of decisions the attention router couldn't auto-apply with confidence. High-confidence machine work is already applied (shown collapsed, one-click undo); your eyes go only to the ambiguous and the business-meaning-dependent. Every item carries a **suggested answer pre-filled** and an **accept / edit / reject** control; `A` accepts all remaining high-confidence items at once; the whole queue is keyboard-drivable so a full pass is keystrokes, not mouse-hunting. The readiness meter at the top updates live as you go.

For each synthesized candidate the reviewer sees, side by side: the natural-language question, the gold SQL, and **the actual executed result rows**, plus controls to retag difficulty, mark a question as *canonical* vs a phrasing variant, and edit either the question or the SQL and re-run it live. Because the SQL already executed during synthesis, review is a fast yes/no pass rather than authoring from scratch.

The same console handles every other decision the loop produces: **judge verdicts** (confirm or override each LLM-judge call on a benchmark row, with its rationale shown — see §7), **Coach proposals** (accept/apply the §8 diffs visually), **eval-run failures** (inspect question + shown context + generated SQL + judge rationale, and promote a now-fixed case straight into the benchmark), and **incoming real-user questions** from `serve` (one click to add to the golden set). One surface for synthesis, evaluation, judging, and iteration — you never leave the platform to maintain golden data.

---

## 7. Evaluation harness

Goal: replicate Genie/Mosaic benchmark scoring with *open, editable* judge prompts, and run it locally against any DB. Each benchmark question runs as a **fresh, stateless** conversation (no thread context), then passes through layered scorers — modeled on Databricks' Genie benchmark evaluator decomposition.

**Layer 1 — deterministic / code scorers (cheap, run always)**
- `syntax_validity` — does the generated SQL parse / `EXPLAIN` cleanly?
- `schema_accuracy` — do all referenced tables/columns exist? (catches hallucinated columns)
- `asset_routing` — when a trusted asset *should* have answered, did the agent use it?
- `result_correctness` — **execute** gold SQL and generated SQL, compare result sets (order-insensitive set comparison, with numeric tolerance and column-aliasing normalization). This is the primary, objective signal — the same "compare result sets" approach Genie uses. Exact-match → **Correct**; mismatch or no gold → routed to Layer 2 / **manual review**.

**Layer 2 — LLM judges (Claude; only when needed — e.g. result mismatch, no gold SQL, or fuzzy questions)**
- `semantic_equivalence` — are gold SQL and generated SQL logically equivalent despite different result rows (e.g. extra columns, different rounding)? Catches "different SQL, same intent."
- `logical_accuracy` — does the SQL correctly implement the question's intent given the schema?
- `completeness` — does the answer fully address the question (no missing filter/group-by)?
- `answer_quality` — if an NL summary was produced, is it grounded in the rows and correct? Uses the optional `eval_note` as grading guidance (Genie's "evaluation note").

**Arbiter.** When Layer-1 result-correctness already passes, skip the expensive judges (zero LLM cost on passing rows). When deterministic and LLM scorers disagree, an arbiter pass adjudicates and flags low-confidence cases as **manual review needed** rather than silently scoring them.

**Outputs.** Per-question verdict + rationale; per-run aggregates **reported separately for the dev set and the held-out test set** (accuracy, % manual-review, mean cost, mean latency, token usage); the **run's model versions** for every role (so a score is never divorced from the model that produced it — §11); and a **diff vs the previous run** so you see exactly which questions a change fixed or broke (regression detection). A dev↔test accuracy gap above a threshold prints an overfitting warning. Stored in `.sqbyl/runs/`.

Judge prompts live in editable files (`judges/*.md`) so you can audit and tune them — the opposite of a hosted black-box judge.

**Human-in-the-loop over the judge.** Every LLM-judge verdict is reviewable, not final. In the review console (§6.5) each judged benchmark row shows the question, the generated SQL, the gold answer, and the judge's verdict **with its rationale**; the human clicks **confirm** or **override** (flip the verdict, optionally with a note). Overrides do triple duty:
1. they become the authoritative result for that run, so the headline accuracy number is human-trusted, not judge-asserted;
2. they accumulate into a **calibration set** that yields a live judge↔human agreement score (the metric Databricks reports for its own judges), so you know exactly how much to trust the judge on unreviewed rows;
3. they can be injected back into the judge prompt as few-shot examples — i.e. the judge gets *coached* the same way the agent does.

This is the project's unifying pattern: **the LLM proposes, the human reviews, and the correction improves the system** — applied identically to the agent (Coach), the benchmarks (synth review), and the judge (verdict override). Rows the arbiter marks *manual review needed* are simply the ones surfaced first.

---

## 7.5 Operational KPIs & reporting — the numbers a team reports up

Everything sqbyl already meters and traces — `.sqbyl/usage.db` (every paid call, §9), `.sqbyl/runs/` (eval aggregates, §7), the OTel traces (latency, §3) — is the raw material for the metrics a user's *organization* needs, not just the curator at the keyboard. `sqbyl report` (Python: `proj.kpis()`) rolls those local stores into a **`KpiReport`** — a pydantic model like every other artifact (§4), emitted as a human-readable table **and** machine JSON so it pipes straight into a team's BI/dashboards or a finance spreadsheet. It is a *reporting view over data already captured*, not new collection: no extra tokens, no extra DB reads, and **aggregates only — never row data** (§13). Cost and quality are reported **separately for dev and the held-out test set** (§7), never conflated.

The headline is **token unit cost** — what each answered question actually costs — because that is the number that makes "is this defensible to deploy at scale?" concrete. Four families, each aimed at a different stakeholder:

- **Unit economics (finance).** Token unit cost: **\$/query** and **tokens/query**; cost per release build; **cache-hit savings %** (prompt-cache reads vs. cold); and a **projected run-rate** (\$/month at a stated query volume) so a team can budget a deployment before shipping it.
- **Quality (the curator / data team).** Held-out **accuracy**, **% needing manual review**, **self-repair rate** (answers that needed a retry — a leading indicator of brittle context), **failure/refusal rate**, and the **dev↔test gap** (the overfitting signal, §7) surfaced as a first-class KPI rather than a footnote.
- **Performance (engineering / SRE).** Per-query **latency p50/p95** and throughput, read straight from the OTel spans (§3) — the same data any OTel backend would chart, so this never locks observability in.
- **Process & readiness (product / leadership).** **Round-trips-to-ship** (the §1.5 product metric — minimizing messages/clicks is tracked "like accuracy"), the **readiness score** (§5.5 distance-to-shippable), and the **accuracy/cost trend across releases** (the optimizer frontier, §6.C) so improvement over time is legible, not anecdotal.

This is the same posture as the spend meter (§9): make the economics and quality of the agent *legible and exportable* rather than asking a team to reverse-engineer them from logs. It is downstream of the eval harness (quality KPIs need §7 runs) and the cost machinery (§9), so it fills in as those land — see the implementation plan.

---

## 8. The Coach — LLM-assisted iteration (headline feature)

The thing the user specifically wants: *Databricks recommends instructions and prompt tweaks; the open-source version should do the same, using the same Claude model and key.*

**Input to the Coach (per eval run):** for each failing or manual-review question **in the dev set** (the Coach is never shown `test.yaml`) — the NL question, retrieved context that was actually shown to the agent, the agent's chain-of-thought, its generated SQL, the gold SQL/asset, the scorer verdicts and rationales, and any execution error. Plus the *current* project files and the inherited best-practice rubric (examples > semantics > prose; keep instructions minimal and non-conflicting; prefer trusted assets; small focused table set).

**What the Coach does:** clusters the failures by root cause (e.g. "model doesn't know `status='refunded'` should be excluded from revenue" → a *semantics/measure* gap, not a prose gap) and proposes the *minimal, highest-leverage* edit at the *right layer of the hierarchy*. It deliberately avoids reaching for text instructions when a column description, synonym, measure, or example would fix it — and it flags *conflicts* it introduces.

**Output: a ranked list of concrete, applyable proposals.** Each is a file diff, not advice. Example:

```
sqbyl Coach — run 2026-06-29T14:02 · 7/30 failing · est. fix value shown

[1] Add measure `net_revenue` to semantics/orders.yaml          fixes ~3 Qs  (high)
    Root cause: agent summed amount_cents without excluding refunds on 3 revenue Qs.
    + measures:
    +   - name: net_revenue
    +     sql: "SUM(CASE WHEN status='confirmed' THEN amount_cents ELSE 0 END)/100.0"

[2] Add column synonym 'churn' for customers.is_active           fixes ~2 Qs  (high)
    Root cause: 'churned customers' didn't map to is_active=false.

[3] Add few-shot example for quarter-over-quarter growth         fixes ~1 Q   (med)
    + examples/growth.yaml (new)

[4] Instruction tweak: clarify fiscal year starts in February    fixes ~1 Q   (low)
    ⚠ touches global instructions — applied last, after data/example fixes.
    (Note: this mirrors Databricks' own fiscal-year example.)

Apply which? [1,2,3 / all / none / explain N / edit N]
```

**Workflow:**
```
sqbyl eval dev                         # run → 23/30
sqbyl coach                            # review proposals above
sqbyl coach apply 1 2 3                # writes the diffs to your files (git tracks them)
sqbyl eval dev                         # re-run → 29/30, diff shows which Qs flipped
```

Because every proposal is a file diff under version control, the human stays in control, the change history is auditable, and a bad suggestion is one `git revert` away — none of which is true inside a hosted optimizer. The autonomous `sqbyl optimize` (mode C) simply automates the `coach → apply → eval` loop with a keep-if-it-helped policy and a spend cap.

**Why "same model, same key" matters here.** The Coach reasons about *the agent's own behavior*, so using the same Claude model means its mental model of how the agent will interpret an edit is well-calibrated to the agent it's editing. One key, per-role models (§9) — with the option to pin an independent judge model if you want judge independence from the thing being graded.

---

## 9. Anthropic API integration

- **Single credential.** `ANTHROPIC_API_KEY` via env (never in files). One key powers every role — and because context selection is LLM/lexical rather than vector-based, there is genuinely no second provider (no embeddings key) to manage.
- **One key, per-role models.** Each role — `agent`, `selection`, `orchestrator`, `synth`, `coach`, `judge` — has its own independently pinnable model id in `sqbyl.yaml`, falling back to a single `default`. Pin a cheaper model for high-volume orchestration/synth and a stronger one for the agent, or a different family for the judge to get independence from the thing being graded. Default judge == agent for simplicity; the bias tradeoff is documented and independence is a one-line change.
- **Automation is configurable.** `automation.auto_judge` / `automation.auto_coach` control whether the loop runs itself after an eval. When on (default), proposals and judge verdicts are pre-computed and waiting; when off, sqbyl still prints a one-line nudge pointing at `sqbyl judge` / `sqbyl coach`, so the capability stays discoverable without being forced.
- **Prompt caching.** The compiled schema/semantics block is large and stable across a benchmark run — cache it so a 30-question eval doesn't resend the schema 30×. Major cost lever.
- **Structured outputs.** SQL generation, judge verdicts, and coach proposals use tool/`response`-style structured returns (strict JSON: `{plan, sql}` / `{verdict, rationale}` / `{proposals:[...]}`) and are parsed defensively.
- **Cost: estimated before, metered during, capped throughout.** Every paid command prints an **up-front token/\$ estimate** (from the planned call count × model rates) before spending, shows a **live spend meter** while running, and meters every call to `.sqbyl/usage.db` after. `--budget $N` is accepted by `init`, `eval`, `synth`, and `optimize`: in guided runs it pauses and asks before exceeding the cap; in `--auto`/CI it hard-stops. `sqbyl cost <command>` (or any command with `--dry-run`) returns the estimate without spending a cent. This is what makes the "quality vs cost frontier" — and "no surprise bill" — real rather than aspirational.
- **Model-agnostic seam.** A thin `LLMClient` interface so the same project can later target a local/OSS model for any role while keeping Claude elsewhere — but Anthropic is the first-class, documented path.

---

## 9.5 Implementation tooling & testing sqbyl itself

sqbyl's pitch to contributors is "everything is readable and editable" — which only holds if the project is itself approachable and well-tested. The whole spec is about evaluating the *user's* agent; this section is about not shipping a tool whose own correctness is unmeasured.

- **Modern Python baseline.** `uv` for env/dependency management, `ruff` for lint+format, `pytest` for tests, full type hints, and **pydantic v2** as the backbone for every project-file and release-artifact schema (per §4) — which also auto-generates the published JSON Schema.
- **Deterministic tests without burning tokens.** The `LLMClient` seam (§9) is the test seam too: a **mock/recorded client** lets the context compiler, scorers, run-diffing, release compile/load, and read-only guard be tested deterministically with zero API spend. Record-replay fixtures cover the few paths that need real model output.
- **A seeded fixture database.** A checked-in **DuckDB** file with the `orders`/`customers` schema from §4 gives every test (and every new contributor) a real database to introspect, synth against, and eval on in seconds — no external DB, no credentials. It doubles as the example project in the README.
- **The dogfood project.** That fixture ships as a complete example sqbyl project (semantics + dev/test benchmarks) so `sqbyl init`/`eval`/`coach` have an end-to-end smoke test that runs in CI against recorded model responses.
- **Observability for free.** Because traces follow the OTel GenAI conventions (§3), the same traces that feed the Coach can be inspected in any OTel viewer during development.

---

## 10. Interfaces

### CLI
```
# ── the push: guided by default (§5.5) ──
sqbyl init <db-url>              # guided: free profile pass → costed plan → confirm → step through
                                 #   (--auto --budget $5 for headless/CI; --dry-run to estimate only)
sqbyl review                     # the attention queue + golden-set/judge/proposal review (web UI)
sqbyl release create --tag v3    # bless current version → portable JSON (§11)

# ── à la carte (each push step, for power users / CI) ──
sqbyl introspect                 # read DB schema → draft semantics/*.yaml
sqbyl profile                    # read-only column stats → profile: blocks ($0, no LLM)
sqbyl annotate [--llm]           # Claude drafts descriptions/synonyms/labels (grounded in profile)
sqbyl synth [--n 40] [--budget]  # execution-grounded candidate Qs + verified gold SQL → dev set
sqbyl eval [dev|test|<path>] [--budget]   # run eval harness → scored report + run diff
                                 #   (auto-judges/auto-coaches per automation config; else nudges)
sqbyl judge                      # run/re-open LLM-judge verdicts (when auto_judge is off)
sqbyl coach [apply N...]         # re-open / apply the pre-computed context edits (dev only)
sqbyl optimize --budget $5 --target 0.9   # autonomous loop on dev; reports held-out test
sqbyl cost <command> | <cmd> --dry-run    # estimate $ / tokens for any paid command, spend nothing
sqbyl ask "..."                  # one-shot NL→SQL→result (interactive REPL: sqbyl chat)
sqbyl runs                       # list runs; sqbyl runs diff <a> <b>
sqbyl report [--json] [--volume N]   # roll up usage/runs/traces → KpiReport (§7.5)
                                 #   token unit cost, accuracy, latency, readiness — dev vs test

# ── ship & serve ──
sqbyl run <release.json>         # serve a release: inject DB + key; exposes ask()/REST/MCP
sqbyl serve                      # local web chat against the working project
```

### Python
```python
from sqbyl import Project

proj = Project.load("./my-project")
ans = proj.ask("net revenue last month")
print(ans.sql, ans.rows)

report = proj.eval("dev")                    # → ScoredRun (held-out: proj.eval("test"))
kpis = proj.kpis(volume=10_000)              # → KpiReport: $/query, accuracy, p95, readiness (§7.5)
proposals = proj.coach(report)               # → list[Proposal]
proj.apply(proposals[:3])                    # writes diffs to project files
proj.eval("dev")                             # re-measure

cands = proj.synth(n=40)                      # execution-grounded candidates (review in UI)
rel = proj.release(tag="v3")                  # → portable artifact stamped with the scorecard

# ...elsewhere, in production — model + DB injected, brain unchanged:
from sqbyl_runtime import load
agent = load("revenue-analytics-v3", db=env.DATABASE_URL, model="claude-opus-4-8")
agent.ask("net revenue last month")
```

The Python API is the substrate; the CLI is a thin wrapper; `sqbyl serve` exposes the same `ask`/feedback loop over HTTP so a project can back a real chat app (and downvotes from real users feed the Coach, replicating Genie's monitoring→re-curation loop).

---

## 11. Release & promotion — blessing a version and shipping it

Once your benchmark score is where you want it, you need to be able to say *"this version — these annotations, labels, prompts, examples — is the one,"* and hand it to production. The thing you ship is the **curated context, not a frozen runtime.** sqbyl makes that distinction the core of release:

**The release (the "brain") — immutable, portable, what you ship:**
- the semantic layer + every annotation and column label
- instructions / prompts
- examples and trusted assets
- context-selection / value-matching config and judge prompts
- a manifest: a version tag + timestamp + the **eval scorecard** from the run that justified promotion (which benchmark, accuracy, cost, latency)

**Supplied by the target environment (the "body") — injected at load time, never baked in:**
- the model + API key (swap Claude for anything — not part of the release)
- the database connection (staging DB, prod replica — your call)
- runtime resources

`sqbyl release create --tag v3` compiles the working project into **a single self-contained JSON file** (`revenue-analytics.v3.json`) stamped with the score that made you pick it. JSON because this is an open-source, public project: a release is a portable, human-readable, diffable artifact anyone can email, drop in object storage, commit, or load from another language — not a tool-specific binary. The schema is a **documented, versioned public interface** (`schema_version` in the manifest), so third parties can read, generate, or serve releases without sqbyl itself. The artifact is the unit you move to production. There is deliberately **no model freezing, no output-replay, and no CICD opinion** — you explicitly want to stay free to change the model or the database, and the brain/body split is what makes that safe. "Promote" is just choosing which release JSON is live; in the simplest case it is "copy this file to prod, set its `DATABASE_URL` and `ANTHROPIC_API_KEY`, done."

A release JSON is roughly:
```json
{
  "schema_version": 1,
  "name": "revenue-analytics",
  "tag": "v3",
  "created_at": "2026-06-29T14:02:00Z",
  "scorecard": { "benchmark": "test", "accuracy": 0.94, "n": 50,
                 "dev_accuracy": 0.97, "dev_n": 120,
                 "human_reviewed": true, "judge_human_agreement": 0.97,
                 "blessed_with_models": { "agent": "claude-opus-4-8",
                                          "judge": "claude-opus-4-8" } },
  "dialect": "postgresql",
  "schema_fingerprint": "sha256:…",
  "semantics": [ /* tables, columns, labels, joins, measures, filters */ ],
  "instructions": "…",
  "examples": [ /* NL → SQL */ ],
  "trusted_assets": [ /* named, parameterized SQL */ ],
  "judges": { /* judge prompts */ },
  "selection": { /* large-schema context-selection config */ }
}
```

The headline `accuracy` is the **held-out test** number — the only one that wasn't optimized against — with the `dev_accuracy` shown beside it so a reviewer can see the gap. `blessed_with_models` records exactly which model produced that score, because an accuracy number is only meaningful for the model that generated it (and the brain/body split invites loading the same brain under a different model). The whole `schema_version`'d shape is **generated from the pydantic models** of §4, so "documented, versioned public interface" is something the build emits, not something a human keeps in sync by hand.

**Loading it elsewhere — the runtime is just a model with logs.** Downstream, the agent should feel like any other model object: load it, call it, serve it from whatever you already have. That's a **separate, dependency-light package, `sqbyl-runtime`** — it contains *only* load + `ask()` + structured logging. None of the dev machinery (eval harness, synth, Coach, judges, the review console) ships with it or is even importable; that all lives in the full `sqbyl` package back in your dev repo. So adding a sqbyl-backed endpoint to an existing prod API is three lines:

```python
from sqbyl_runtime import load
agent = load("revenue-analytics-v3.json", db=env.DATABASE_URL, model="claude-opus-4-8")

@app.post("/ask")                       # your API, your auth, your scaling
def ask(q: str):
    return agent.ask(q)                  # → {plan, sql, rows, used_assets, usage, latency}
```

The recommended production pattern is exactly this **library embed**: the agent lives inside your existing service and inherits its auth, connection pooling, secrets, and observability — you operate nothing new. (`sqbyl run <release>` / `sqbyl serve` exist for non-Python callers and quick HTTP exposure, but they are intentionally *not* hardened — auth, pooling, rate-limiting, and multi-tenancy are the host's job, so don't put `sqbyl serve` on the open internet expecting otherwise.) On load the runtime does two cheap, non-fatal checks: **warn on schema mismatch** (a renamed table is the one thing that silently breaks a shipped agent) and **warn on model mismatch** against `blessed_with_models` (the scorecard was earned on a specific model). Both respect "I might point it at a different DB / model" while still flagging the footgun.

**Monitoring in prod, iteration back in dev.** The runtime emits the same OTel-structured traces and 👍/👎 feedback as everywhere else (§3) — but in prod that's all it does with them: log for monitoring, no inline eval or coaching. The loop back is explicit and one-directional: export those prod traces, drop them into the dev project, and they become synth candidates and eval cases (§6.A) for the *next* version. So production stays lightweight — a model and its logs — while post-ship usage still feeds the next round of curation without any dev weight running live.

**Interop, if you want it.** Portability comes from the release artifact, *not* a framework — the core stays dependency-light and you keep control of behavior. The same release can also be exported as a LangChain chain/tool, an **MCP server**, or a plain callable. These are export *shapes* of the one release, not the foundation; building the core on LangChain would couple your shipped agents to its churn for no portability gain.

---

## 12. Build order

**Milestone 0 — Engine.** Introspect + **column profiler** + semantic YAML + context compiler + agent runtime (generate, validate, execute, self-repair) for DuckDB + Postgres. `sqbyl ask` works end-to-end. (The runtime ships as the minimal, dependency-light `sqbyl-runtime` package from day one — the dev toolkit depends on it, not vice versa, so the "model with logs" you ship downstream is never bloated by eval/synth/coach.)

**Milestone 1 — Golden set + Evaluation.** Execution-grounded `sqbyl synth`, the `sqbyl review` console, the eval-set format, and the harness with Layer-1 deterministic scorers + result-set comparison + run diffs. You can now build a golden set fast and *measure* against it.

**Milestone 2 — Coach.** LLM judges (Layer 2) + the Coach with applyable diffs, reviewable in the console. This is the moment sqbyl becomes differentiated; prioritize it over breadth of DB support.

**Milestone 2.5 — The push.** The orchestrator (parallel fan-out, partial-failure tolerance) + attention router (confidence scoring, auto-apply, leverage-sorted queue) + readiness meter + the **guided, cost-gated `sqbyl init`** (free profile pass → costed plan → confirm → stepped enrichment with a live spend meter; `--auto --budget` for CI) and the **estimate/`--budget`/`--dry-run`** machinery across paid commands (§5.5, §9). This is the product's whole reason to exist over rolling your own — treat it as a headline milestone, not polish.

**Milestone 3 — Release + Optimizer.** `sqbyl release` / `sqbyl run` with the brain/body split and scorecard stamping, plus the autonomous budgeted optimization loop and version frontier.

**Milestone 4 — Surface & scale.** `sqbyl serve`, MCP/LangChain export adapters, LLM/lexical context selection for large schemas/example banks, lexical value-matching, importers (dbt models, query logs), more dialects (Snowflake/BigQuery/MySQL).

---

## 13. Open questions / risks

- **Judge trust.** LLM judges grading an LLM agent of the same family risks shared blind spots. Mitigated by the deterministic result-set scorer as the primary signal, the **human confirm/override loop** that turns judge verdicts into a measured judge↔human agreement score and feeds corrections back as calibration (§7), and an optional independent judge model.
- **Benchmark leakage / overfitting.** sqbyl's loop edits context to push a score up, so optimizing and measuring on the same set would be training on the test set. Handled structurally by the **dev/held-out test split** (§4): synth feeds dev, Coach/Optimizer iterate on dev, and the held-out `test.yaml` — ideally hand-authored — is read only by `eval` and humans. The released scorecard headlines the held-out number with the dev number beside it, and a large gap prints an overfitting warning (§7, §11).
- **Read-only safety.** Default to SELECT-only and refuse non-SELECT unless explicitly opted in; never let the Coach or agent issue DDL/DML. Note this is best-effort at the SQL layer — for real isolation, on connect sqbyl inspects the credential's privileges and **warns (with a suggested fix) if `DATABASE_URL` can write**, recommending a dedicated read-only DB role. Like Genie/Agent Bricks, sqbyl does not attempt to solve database-level access control itself; that's the host DB's job.
- **Profiling cost & PII.** The column profiler is deterministic SQL, but on huge tables a naive `COUNT(DISTINCT)`/percentile scan is expensive — so it samples (`TABLESAMPLE` + row caps) and degrades to cheaper stats rather than full scans. And min/max plus captured sample values can surface PII (salary ranges, real emails); profiling is read-only and per-column/per-table opt-out (`profile: false`), and raw `sample_values` can be suppressed while keeping the non-identifying stats.
- **Surprise spend.** The failure mode of an LLM-heavy "do it all" tool is an unexplained bill. Mitigated by the guided, cost-gated `init` (free deterministic work first, then a confirmed estimate), up-front estimates + live spend meters + `--budget` on every paid command, and `--dry-run`/`sqbyl cost` to price anything before running it (§5.5, §9). `--auto` requires `--budget` and hard-stops.
- **Gold-SQL drift.** `now()`-relative gold answers move over time. Support frozen "as-of" execution or relative-window normalization in the comparator.
- **Large schemas.** Beyond ~30 tables, "include everything" breaks; **context-selection quality** (which tables/examples Claude shortlists) becomes the accuracy bottleneck (Genie caps at 30 tables / nudges toward ≤5 for a reason). Treat schema selection as a first-class, separately-evaluable component — and note it is LLM/lexical, not vector retrieval, so it has its own token cost rather than an embedding dependency.
- **Semantic-layer authoring cost.** The honest tradeoff vs Genie: with no Unity Catalog, *someone* must author metadata. `sqbyl introspect`/`annotate` + the Coach exist specifically to amortize that cost, but it won't be zero.
