# sqbyl — Technical Implementation Plan

*A chronological, phase-by-phase build sequence for developing sqbyl with Claude Code. Companion to `sqbyl-design-spec.md` and `sqbyl-user-journey.md`. Optimized for ordering and dependency-unblocking, not calendar scheduling.*

---

## How to read this plan

- **Phases** are ordered so that each one is buildable *only using things already built in earlier phases*. Nothing forward-references.
- Each phase lists **Steps** sized roughly to one focused Claude Code working session (one coherent PR). Each step states what it builds, what it depends on, and **how it's verified deterministically** — because the design spec's own testing discipline (§9.5) says correctness must be measured, and because Claude Code works best when each unit has a checkable exit condition.
- The guiding rule throughout: **the `LLMClient` mock seam and the pydantic models come first**, so that everything built after them is testable with zero API spend.
- Symbols: 🧱 = foundational/blocking, 🔌 = LLM-touching (needs mock + record-replay), 🖥️ = UI, 📦 = packaging boundary.

The phase order maps onto the spec's milestones but front-loads the cross-cutting foundations (Phase 0) that the milestones assume:

| Spec milestone | Plan phases |
|---|---|
| (implicit foundations) | **Phase 0** |
| M0 — Engine | **Phases 1–2** |
| M1 — Golden set + Eval | **Phases 3–4** |
| M2 — Coach | **Phase 5** |
| M2.5 — The push | **Phases 6–7** |
| M3 — Release + Optimizer | **Phase 8** |
| M4 — Surface & scale | **Phase 9** |

---

## Phase 0 — Foundations (everything else depends on this)

This phase ships no user-facing feature. It exists so that every later phase is testable, parallelizable, and dependency-light. **Do not skip or compress it** — the spec's whole credibility ("everything readable, editable, tested") rests here.

### 0.1 🧱📦 Repo + tooling skeleton
- `uv` workspace with **two packages**: `sqbyl-runtime` (minimal) and `sqbyl` (full toolkit). `sqbyl` depends on `sqbyl-runtime`, never the reverse — enforce this with an import-linter rule so the dependency arrow can't silently flip.
- `ruff` (lint+format), `pytest`, `mypy`/pyright strict, pydantic v2 pinned.
- CI: lint → type-check → test, plus the import-direction check.
- **Done when:** `uv run pytest` passes on an empty suite and CI is green.

### 0.2 🧱 Pydantic v2 models — the single source of truth (spec §4, §11)
Model *every* project-file and release-artifact shape as pydantic v2 before writing any logic that reads/writes them:
- `SqbylManifest` (`sqbyl.yaml`), `TableSemantics` + `Column` + `Profile` + `Join` + `Measure` + `Filter`, `Example`, `TrustedAsset`, `BenchmarkQuestion`, `JudgePrompt`, `SelectionConfig`.
- `ReleaseArtifact` (the §11 JSON) with `schema_version` and `scorecard`.
- Wire up **auto-generated JSON Schema** export from these models (a `sqbyl schema export` dev command or a build hook). This *is* the published public interface promised in §4/§11 — generate it, don't hand-maintain it.
- **Done when:** round-trip tests (`model → YAML/JSON → model`) pass for every shape; generated JSON Schema is checked into the repo and a test fails if it drifts from the models.

### 0.3 🧱🔌 The `LLMClient` seam (spec §9, §9.5)
- A thin `LLMClient` interface: `complete(messages, *, model, tools/response_format) -> structured result`, with usage/token accounting baked into the return.
- Three implementations: **real Anthropic client**, **mock client** (scripted/deterministic responses for unit tests), **record-replay client** (captures real responses to fixtures for replay in CI).
- Prompt-caching and structured-output (strict-JSON tool-use) support live *inside* this seam so callers never hand-roll either.
- **Done when:** a trivial caller can be unit-tested against the mock with zero network, and a record-replay fixture can be captured and replayed.

### 0.4 🧱 Seeded DuckDB fixture + dogfood project (spec §9.5)
- A checked-in **DuckDB file** with the `orders`/`customers` schema and realistic-enough data (so profiling produces meaningful ranges/distincts).
- A complete example sqbyl project against it (semantics + dev/test benchmarks) — this becomes both the README example *and* the end-to-end CI smoke test.
- **Done when:** tests can open the fixture DB and read its schema; the dogfood project deserializes cleanly into the Phase 0.2 models.

### 0.5 🧱 Usage/state registry scaffold (spec §3 #7)
- `.sqbyl/` layout: SQLite + JSONL for traces, run history, usage accounting, and project content-hashes (so runs link to the exact config that produced them).
- Trace records shaped to **OpenTelemetry GenAI semantic conventions** from day one (cheap now, painful to retrofit).
- **Done when:** a usage row and a trace row can be written and read back; content-hash of the dogfood project is stable across runs.

> **Exit criteria for Phase 0:** every later phase can be developed and tested without a live API key or external database.

---

## Phase 1 — Read-only engine core (no LLM yet)

Goal: get all the **free, deterministic** machinery working first — exactly mirroring the product's "free pass before any spend" principle. None of this costs tokens, so all of it is fully unit-testable now.

### 1.1 🧱 DB connection layer + read-only guard (spec §1, §13)
- SQLAlchemy-based connector for **DuckDB + Postgres** (the two M0 dialects). Dialect abstraction kept thin but real.
- Read-only enforcement at the SQL layer (refuse non-SELECT) **and** the privilege check that warns if the credential can write, with the suggested-fix message.
- **Done when:** connecting with a writable role emits the warning; a non-SELECT statement is refused; tested against the DuckDB fixture.

### 1.2 🧱 Schema introspector
- Read tables, columns, types, PK/FK, existing comments → draft `TableSemantics` objects (no descriptions yet).
- For FK-less DBs, emit **heuristic** join candidates (name/type matching) as low-confidence stubs.
- **Done when:** introspecting the fixture reproduces its known schema into models; `sqbyl introspect` writes draft `semantics/*.yaml`.

### 1.3 🧱 Column profiler (spec §3.1, §13)
- Deterministic, read-only aggregate SQL per column: null fraction, distinct count, min/max/range, percentiles, top-k for low-cardinality.
- **Sampling discipline** built in from the start: `TABLESAMPLE` + row caps on large tables; degrade to cheaper stats rather than full scans. Per-column/table `profile: false` opt-out for PII.
- Writes `profile:` blocks into the semantic YAML.
- **Done when:** profiling the fixture yields correct stats; a large-table path provably uses sampling, not a full scan; opt-out suppresses raw `sample_values`.

> This is the cheapest, highest-trust part of the system and it has **no LLM dependency** — building it first means Phase 2's annotator has real grounding data to consume, just as the spec insists.

---

## Phase 2 — Agent runtime (first LLM calls) → `sqbyl ask` works end-to-end

Goal: close out **Milestone 0**. After this phase, a human-authored project can answer a question. This is the first phase that spends tokens, so it leans hard on the Phase 0.3 mock seam.

### 2.1 🔌 Context compiler (spec §3 #2, §5 steps 1–2)
- Compile project files + question → prompt context: selected tables, annotated DDL, relevant measures/filters/examples/trusted assets, instructions.
- **Small-project path only for now**: "include everything." (LLM/lexical shortlisting for large schemas is deferred to Phase 9 — don't build it yet.)
- Wire **prompt caching** of the stable schema/semantics block here.
- **Done when:** for the dogfood project, the compiled context is a deterministic, snapshot-tested string given fixed inputs.

### 2.2 🔌 Agent pipeline (spec §5 steps 3–7) — **lands in `sqbyl-runtime`**
Build the stateless `ask()` pipeline, and build it **inside `sqbyl-runtime`** so the shippable "model with logs" is correct from day one (spec §12 M0 note):
1. Generate — chain-of-thought plan + candidate SQL via structured output.
2. Static-validate — `EXPLAIN`/parse against live schema (no execution).
3. Execute — read-only.
4. Self-repair — feed errors back up to `self_repair_attempts`.
5. Respond — `{plan, sql, rows, used_assets, usage, latency}`, citing trusted assets when used.
- Every run writes an OTel-shaped trace (Phase 0.5).
- **Done when:** against recorded model responses, `ask()` answers the dogfood questions end-to-end; self-repair is exercised by a fixture that returns bad-then-good SQL.

### 2.3 🔌 Annotator (spec §3 #1) — `sqbyl annotate`
- Claude drafts descriptions, synonyms, `sample_values`, table descriptions — **grounded in the Phase 1.3 profile**, not guessing from names.
- Per-table parallelizable (real fan-out comes in Phase 6; here it can be sequential).
- Each annotation carries a **confidence** (consumed later by the attention router).
- **Done when:** annotating the (stripped) fixture produces sensible descriptions under record-replay; confidence is populated.

> **Milestone 0 complete.** CLI surface so far: `introspect`, `profile`, `annotate`, `ask`. `sqbyl-runtime` can already load a hand-built project and answer.

---

## Phase 3 — Eval harness, deterministic layer first (spec §7)

Goal: be able to **measure**. Build the cheap, objective scorers before any LLM judge — they're the primary signal and fully deterministic.

### 3.1 🧱 Eval-set format + runner
- `benchmarks/dev.yaml` + `benchmarks/test.yaml` (same schema; **dev/test separation enforced structurally** — see 3.4).
- Runner executes each question as a **fresh, stateless** `ask()` conversation.
- **Done when:** the runner executes the dogfood dev set against recorded responses.

### 3.2 🧱 Layer-1 deterministic scorers (spec §7 Layer 1)
- `syntax_validity`, `schema_accuracy`, `asset_routing`, and the headline **`result_correctness`** (execute gold + generated SQL, order-insensitive set compare, numeric tolerance, column-alias normalization).
- Handle **gold-SQL drift** (`now()`-relative answers): support as-of / relative-window normalization in the comparator from the start (spec §13).
- **Done when:** known-correct and known-wrong fixtures score correctly; a `now()`-relative gold question scores stably across two "dates."

### 3.3 🧱 Run reports + run diffs
- Per-run aggregates (accuracy/cost/latency/token usage), **reported separately for dev and test**, stamped with **the model version for every role** (a score is never divorced from its model).
- **Diff vs previous run** — exactly which questions flipped (regression detection). Stored in `.sqbyl/runs/`.
- **Done when:** two runs produce a correct flipped-questions diff; reports persist and reload.

### 3.4 🧱 Dev/test guardrail
- Make the held-out set structurally unreachable by anything but `eval` and humans: synth writes only `dev`; coach/optimize read only `dev`. Encode this as a code-level access boundary, not a convention.
- Surface the **dev↔test gap as an overfitting signal** in reports.
- **Done when:** a test asserts that coach/synth/optimize code paths *cannot* read `test.yaml` (e.g. they don't receive it).

---

## Phase 4 — Synthesizer + the review console shell (spec §6.A, §6.5)

Goal: build the golden set **fast**, and stand up the one UI surface the product needs. Together with Phase 3 this completes **Milestone 1**.

### 4.1 🔌 Execution-grounded synthesizer — `sqbyl synth`
- Draft candidate questions, write gold SQL, **execute it, discard anything that errors or returns empty/degenerate** — only executable questions survive.
- Seed from the semantic layer (measures/joins/filters → question fodder); stratify by difficulty; generate phrasing variants; promote real traces from `ask`/imported logs into candidates.
- Survivors land in the **dev** set only.
- **Done when:** synth against the fixture yields executable candidates under record-replay; degenerate candidates are provably dropped; nothing is written to `test.yaml`.

### 4.2 🖥️ Review console — shell + golden-set review (spec §6.5)
- FastAPI + a small bundled UI; **no cloud, no account**. It is a thin surface over the project files — writes land back in `benchmarks/`, `examples/`, `semantics/`. Not a second source of truth.
- First view: per candidate, show **question + gold SQL + actual executed rows**, with accept/edit/reject, retag difficulty, mark canonical vs variant, edit-and-re-run-live.
- Keyboard-driven (`a`/`e`/`r`, `A` accept-all). This interaction model is reused for every later queue, so get it right once.
- **Done when:** a synthesized candidate can be accepted in the UI and the change appears in `benchmarks/dev.yaml` on disk.

> **Milestone 1 complete.** You can synth a golden set in an afternoon and measure against it.

---

## Phase 5 — The Coach + LLM judges (spec §7 Layer 2, §8) — **the differentiator**

Goal: **Milestone 2.** Per the spec, prioritize this over breadth of DB support — it's the moment sqbyl stops being "Vanna with Claude."

### 5.1 🔌 Layer-2 LLM judges (spec §7)
- `semantic_equivalence`, `logical_accuracy`, `completeness`, `answer_quality` — invoked **only when needed** (result mismatch, no gold, fuzzy Q). The arbiter skips judges entirely when Layer-1 already passes (zero LLM cost on passing rows).
- Judge prompts live in **editable `judges/*.md`** files (open, versioned).
- **Arbiter** adjudicates deterministic↔LLM disagreement and flags **manual-review-needed** rather than silently scoring.
- **Done when:** mismatch rows route to judges; passing rows provably skip them; arbiter flags a disagreement fixture.

### 5.2 🖥️🔌 Judge human-in-the-loop (spec §7)
- In the console: each judged row shows question + generated SQL + gold + **verdict with rationale**; human confirms/overrides.
- Overrides do the spec's triple duty: authoritative for the run, accumulate into a **calibration set** → live judge↔human agreement score, and inject back as judge few-shot examples.
- **Done when:** an override flips the run's headline number and the agreement metric updates.

### 5.3 🔌 The Coach (spec §8)
- Input: per failing/manual-review **dev** question — context shown to agent, agent CoT, generated SQL, gold, scorer verdicts, execution error + current project files + the best-practice rubric (examples > semantics > prose).
- Cluster failures by **root cause**; propose the **minimal, highest-leverage edit at the right layer**; avoid reaching for prose; flag conflicts it introduces.
- Output: ranked **applyable file diffs** with rationale + predicted fix-count.
- **Done when:** on a deliberately-broken dogfood project (e.g. missing `net_revenue` measure), the Coach proposes the correct measure diff under record-replay.

### 5.4 🖥️ Coach apply loop
- `sqbyl coach` / `sqbyl coach apply N...` writes diffs to files (git tracks them); reviewable visually in the console.
- **Done when:** the canonical journey works: `eval dev` → `coach` → `coach apply` → `eval dev` shows the targeted questions flip green; every change is a real git diff revertable with `git revert`.

> **Milestone 2 complete.** Build → measure → coach → re-measure loop is real and fully auditable.

---

## Phase 6 — The orchestrator + attention router (the engine behind "the push")

Goal: first half of **Milestone 2.5.** Everything built so far runs sequentially; this phase makes it parallel, partial-failure-tolerant, and attention-routed. Treat as a headline milestone, not polish.

### 6.1 🧱 Orchestrator (spec §3 #8)
- Fan **approved** paid work out concurrently (per-table annotation, join inference, synthesis, baseline eval, fix pre-compute).
- **Bounded, rate-limit-aware** worker pool sized to API tier; retry/backoff on 429s so a 42-table fan-out doesn't self-DoS.
- **Cache-priming:** land the first call that fills the prompt cache *before* releasing the parallel wave.
- **Partial-failure tolerance:** a failed unit becomes a low-confidence card, never a hard stop.
- Single live progress checklist + running spend meter.
- **Done when:** a simulated 429 triggers backoff (not failure); a deliberately-failing unit degrades to a card while siblings complete; cache-prime ordering is asserted.

### 6.2 🧱 Attention router + readiness scorer (spec §3 #9, §5.5)
- Assign confidence to every machine decision; **auto-apply** high-confidence (one-click undo); surface the rest into a single queue **sorted by leverage** (fewest decisions that move readiness most).
- Compute the live **readiness signal** ("86% · 6 decisions to 96%").
- **Done when:** given a set of scored decisions, the queue ordering and the readiness math are unit-tested against expected output.

### 6.3 🖥️ Wire the queue into the console
- The review console (Phase 4/5) now opens onto the **leverage-sorted attention queue**: high-confidence work shown collapsed/applied, ambiguous + business-meaning cards surfaced first, readiness meter live at top.
- Every card is a **decision-with-a-default** (accept/edit/reject), including business-meaning cards with a best-guess pre-filled.
- **Done when:** the dogfood project produces a queue matching the §5.5 mock shape; accepting cards moves the meter live.

---

## Phase 7 — Cost machinery + guided `sqbyl init` (completes the product)

Goal: second half of **Milestone 2.5** — the cost-gating that makes "no surprise bill" real, and the one command that ties the whole push together. This is the product's reason to exist over rolling your own.

### 7.1 🧱 Cost estimation + budget + spend meter (spec §9)
- Up-front **estimate** (planned call-count × model rates) before any paid command.
- **Live spend meter** during; meter every call to `.sqbyl/usage.db` after.
- `--budget $N` on `init`/`eval`/`synth`/`optimize`: guided pauses-and-asks before exceeding; `--auto` hard-stops.
- `sqbyl cost <command>` / `--dry-run` returns the estimate spending nothing.
- **Done when:** `--dry-run` produces an estimate with zero API calls; a budget cap provably halts a run; usage rows reconcile with the meter.

### 7.2 🔌🖥️ Guided `sqbyl init` (spec §5.5)
- The `sam deploy --guided` flow: **Phase 1 free pass** (connect → introspect → profile → heuristic joins, `$0`), then the **costed plan + estimate**, then `[Y]es/[s]elect/[m]odel/[n]o`.
- **Phase 2 stepped enrichment** after confirmation: orchestrated parallel work (Phase 6) surfacing results as it goes, live meter, ending in the attention queue (Phase 6.3).
- `sqbyl init --auto --budget $N` for headless/CI (**`--budget` required** in `--auto`).
- Re-running `init`/`eval` on a changed schema **re-orchestrates only what changed** (content-hash diff from Phase 0.5).
- **Done when:** the full journey-doc flow runs against the fixture under record-replay; `--auto` without `--budget` errors; an unchanged re-run does no paid work.

> **Milestone 2.5 complete.** The headline experience from the user journey works end to end.

---

## Phase 8 — Release, runtime load, and the autonomous Optimizer (spec §11, §6.C)

Goal: **Milestone 3.** Ship a version and let the loop run itself within a cap.

### 8.1 📦 Release artifact — `sqbyl release create` (spec §11)
- Compile the working project into the single self-contained `ReleaseArtifact` JSON (Phase 0.2 model), stamped with the **held-out scorecard**, `blessed_with_models`, `schema_fingerprint`, `schema_version`.
- Headline accuracy = **held-out test**; `dev_accuracy` shown beside it.
- **Done when:** releasing the dogfood project emits a JSON that validates against the generated schema and contains the correct scorecard.

### 8.2 📦 `sqbyl-runtime` load + checks
- `load(release, db=, model=)` in `sqbyl-runtime`: inject DB + model, brain unchanged. **Non-fatal warnings** on schema mismatch and model mismatch vs `blessed_with_models`.
- Confirm the dependency boundary: none of eval/synth/coach/console is importable from `sqbyl-runtime`.
- **Done when:** a release loads and answers under a *different* injected model with the mismatch warning firing; an import test proves dev machinery isn't reachable from the runtime package.

### 8.3 🔌 The Optimizer — `sqbyl optimize` (spec §6.C)
- Autonomous `coach → apply → eval` loop **against dev only**, keep-if-it-helped / revert-if-not, until `--target` or `--budget` hit.
- Returns a **frontier** of versions (accuracy/cost/latency), each a readable git diff; held-out test scored **once** on the picked version; large dev↔test gap → overfitting warning.
- **Done when:** on a fixable broken project, optimize reaches target within budget under record-replay; it provably never reads `test.yaml`; the frontier is returned for selection.

> **Milestone 3 complete.** Release → ship → optimize all work.

---

## Phase 9 — Surface & scale (breadth, last) (spec §12 M4)

Goal: **Milestone 4.** These are deliberately last — none of them is on the critical path to a shippable, differentiated tool, and several (large-schema selection) only matter past the small-space posture the product defaults to.

Order within the phase by likely demand; each is independent and can be parallelized:

### 9.1 🔌 LLM/lexical context selection for large schemas (spec §5.1, §13)
- Claude shortlists relevant tables/examples from a compact catalog; optional lexical narrowing; lexical **value-matching** over high-cardinality columns. **No embeddings / vector store** — stays on the single key.
- Treat schema selection as a **first-class, separately-evaluable** component (its own eval).
- Replaces the "include everything" stub from Phase 2.1 for projects past ~30 tables.

### 9.2 🖥️ `sqbyl serve` + `sqbyl run <release>`
- Local web chat against the working project; serve a release over HTTP/MCP. **Intentionally not hardened** — document that auth/pooling/multi-tenancy are the host's job; don't put `serve` on the open internet.
- Prod 👍/👎 + traces flow back as synth/eval candidates (closes the §7 journey loop).

### 9.3 📦 Export adapters
- LangChain chain/tool, **MCP server**, plain callable — as export *shapes* of the one release, not a foundation. Core stays dependency-light.

### 9.4 🧱 Importers
- dbt models / query logs / existing views → proposed examples + joins.

### 9.5 🧱 More dialects
- Snowflake / BigQuery / MySQL behind the Phase 1.1 dialect seam. SQLite for the lightest tests.

---

## Cross-cutting threads (maintain in every phase, don't bolt on)

These aren't phases; they're invariants that each phase must uphold, because retrofitting any of them is expensive:

1. **Mock-first / record-replay.** Every LLM-touching step (🔌) ships with mock-based unit tests and at least one record-replay fixture. CI never spends tokens. (spec §9.5)
2. **Pydantic is the only schema authority.** No hand-written validation, no hand-maintained JSON Schema. The release interface is *generated*. (spec §4, §11)
3. **Dev/test separation is a code boundary.** Synth/coach/optimize never receive `test.yaml`; enforced by tests, not convention. (spec §4, §13)
4. **OTel-shaped traces from the first trace written.** (spec §3)
5. **Read-only by default + privilege warning** wherever a connection is made. (spec §13)
6. **Cost is estimated-before / metered-during / capped-throughout** for every paid command the moment that command exists — not added in Phase 7 as an afterthought for commands built earlier. (Phase 7 builds the *machinery*; earlier paid commands should route through a stub estimator from the day they're written.) (spec §9, §13)
7. **`sqbyl-runtime` stays minimal.** Anything you build asks: does this belong in the shippable runtime or the dev toolkit? The import-direction lint (Phase 0.1) is the backstop. (spec §11, §12)

---

## Critical-path summary (the spine)

If you build nothing but this spine, you get a working, differentiated tool in the fewest steps:

```
0.2 models  →  0.3 LLMClient mock  →  0.4 fixture DB
      │
1.1 read-only conn → 1.2 introspect → 1.3 profile
      │
2.1 context compiler → 2.2 agent pipeline (in runtime)  ──►  sqbyl ask works
      │
3.1 eval runner → 3.2 deterministic scorers → 3.3 run diffs  ──►  you can measure
      │
4.1 synth → 4.2 review console  ──►  golden set in an afternoon
      │
5.1 judges → 5.3 Coach → 5.4 apply loop  ──►  THE DIFFERENTIATOR
      │
6.1 orchestrator → 6.2 attention router → 7.1 cost → 7.2 guided init  ──►  THE PRODUCT
      │
8.1 release → 8.2 runtime load → 8.3 optimizer  ──►  ship it
```

Everything in Phase 9 hangs off this spine but blocks nothing on it.
