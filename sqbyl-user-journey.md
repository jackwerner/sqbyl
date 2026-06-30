# sqbyl — A First Run

*A short user journey: one person takes a database from "raw tables" to a shipped, benchmarked text-to-SQL agent, and iterates on it. Companion to the design spec.*

Meet **Maya**, a data engineer. Her GTM team keeps pinging her with revenue questions — *"net revenue last month?"*, *"how many active customers in EMEA?"* — and she wants to hand them an agent that writes the SQL itself: accurate, trustworthy, and something she can version like code. Her data is a plain Postgres warehouse. Here's her afternoon.

---

## 1. Setup — two things and a key (5 min)

Install sqbyl and point it at a database and an Anthropic key:

```bash
pip install sqbyl
export ANTHROPIC_API_KEY=sk-ant-...
export DATABASE_URL=postgresql://readonly_user@warehouse.internal/analytics
```

She deliberately uses a **read-only role** in the connection string. When she first tried her admin credentials, sqbyl connected fine but warned: *"this credential can write — consider a dedicated read-only role."* So she swapped it. There's no config file to hand-write; `init` will generate `sqbyl.yaml` for her.

---

## 2. `sqbyl init` — the guided pass (10 min, ~$2)

```bash
sqbyl init
```

sqbyl does the **free, deterministic work first** — connect, read the schema, and profile every column with read-only SQL. No tokens spent yet:

```
▸ connecting…………………………………… done
▸ reading schema………………………………… 42 tables, 380 columns
▸ profiling columns (read-only SQL)… done   ($0 — no LLM)
▸ heuristic join candidates……………… 11 found, 3 ambiguous

Ready to enrich with Claude. Here's the plan and the estimate:
  annotate 380 columns + 42 tables   ~$1.20
  resolve 3 ambiguous joins          ~$0.05
  synthesize ~40-question benchmark  ~$0.60
  baseline eval                      ~$0.30
  ────────────────────────────────────────
  estimated total                   ~$2.15   on claude-opus-4-8

Proceed? [Y]es · [s]elect steps · [m]odel · [n]o
```

She hits **Y**. Because the profiler already measured the data, Claude's drafted annotations are *grounded, not guessed*: it sees `amount_cents` runs 0–4.2M with zero nulls and labels it cents, sees `status` has exactly three values and captures them as sample values. A spend meter ticks against the ~$2.15 she approved. A minute later she lands on a review queue — not a blank page, and not a surprise bill.

---

## 3. Review — confirm the few things only a human knows (5 min)

sqbyl already applied everything it was confident about. It surfaces only the decisions that need her, sorted by how much each moves the score:

```
sqbyl review                         Agent: 86% ▸ 96% in 6 decisions

①  Business meaning — best guess filled in
   "active customer" → customers.is_active = true         [Accept] [Edit] [Reject]
②  Accept measure  net_revenue             fixes 3 Qs ▸ +8%   [Accept] [Edit]
③  Override judge on Q14? rows match gold but judge said WRONG  [Confirm] [Override]
④  Low-confidence join: orders ⋈ shipments on order_id    [Accept] [Edit] [Reject]
```

These are the things the data can't decide on its own — what counts as an *active* customer, whether that join is real. She accepts most, but edits the active-customer definition (her team counts a 90-day window, not just the flag). The readiness meter climbs as she goes. A few keystrokes and she's at her target.

---

## 4. Iterate — measure, coach, re-measure

She runs the benchmark and gets a coached set of fixes for whatever still fails:

```bash
sqbyl eval dev          # 31/36 — shows which 5 questions miss, and why
sqbyl coach             # ranked, applyable file diffs, each with a root cause
sqbyl coach apply 1 2   # writes the edits (git tracks them)
sqbyl eval dev          # 35/36 — the run diff shows exactly which Qs flipped
```

Each Coach proposal is a real diff at the right layer — a missing measure, a synonym, an example — not vague advice, and one `git revert` away if it's wrong. When she's happy with the dev set, she checks the **held-out** set she wrote by hand, which the Coach and optimizer never see:

```bash
sqbyl eval test         # 0.94 — the honest number, with no overfitting hiding in it
```

*(If she wanted to skip the manual loop, `sqbyl optimize --budget $5 --target 0.95` would run coach→apply→re-eval on its own and hand her a frontier of versions to pick from — within a hard cap.)*

---

## 5. Release — bless a version

```bash
sqbyl release create --tag v1
```

Out comes one portable JSON, `revenue-analytics.v1.json` — the agent's **brain**: semantics, instructions, examples, judge prompts, and a scorecard stamped with the held-out **0.94** and the exact model that earned it. The model, key, and database are *not* baked in; they're injected wherever it runs.

---

## 5.5 Report — the numbers her team will ask for

Before she hands this to GTM, Maya's manager will ask two things: *what will it cost us, and how good is it?* She doesn't reverse-engineer that from logs — sqbyl already metered every call and traced every run, so one command rolls it up:

```bash
sqbyl report --volume 10000        # at the team's expected ~10k questions/month
```

```
sqbyl — revenue-analytics  (dev vs held-out test)

unit economics      $0.012 / query · 1.8k tokens / query · 41% cache savings
                    projected run-rate  ~$120 / month  @ 10,000 queries
quality             accuracy   dev 0.97 │ test 0.94   (gap 0.03 — healthy)
                    manual-review 6%  ·  self-repair 8%  ·  failures 1%
performance         latency  p50 1.4s │ p95 2.8s
readiness           96% ▸ shippable   ·  round-trips-to-ship 6   ·  v1 ↑ from v0 (0.86)
```

It's **aggregates only** — never a row of her customers' data — and `--json` pipes the same numbers straight into the team's dashboard. The headline is the **token unit cost**: $0.012 a question makes "can we afford to point the whole GTM team at this?" a one-line answer instead of a guess. The dev-vs-test split is right there too, so the number she reports up is the honest one.

---

## 6. Ship — it's just a model with logs

Maya's app already has an API. Adding a revenue endpoint is three lines, embedding the lightweight runtime — no eval/synth/coach machinery comes along for the ride:

```python
from sqbyl_runtime import load
agent = load("revenue-analytics.v1.json", db=env.DATABASE_URL, model="claude-opus-4-8")

@app.post("/ask")
def ask(q: str):
    return agent.ask(q)     # → {plan, sql, rows, used_assets, usage, latency}
```

It inherits her app's auth, scaling, and observability. In production it just answers and logs — 👍/👎 and traces pile up for monitoring, nothing heavy running live.

---

## 7. The loop closes

Two weeks later, real usage has surfaced questions her benchmark never had. She exports the prod traces, drops them into her dev project, and they become new synth candidates and eval cases. She opens `sqbyl review`, keeps the good ones, re-runs the loop, and cuts `v2`. Production stayed lightweight the whole time; iteration happened back in the full toolkit, exactly where it belongs.

---

**The whole shape, in one line:** connect → profile (free) → confirm a costed plan → review a few human-only decisions → coach to target → release a JSON → report the unit economics → embed it as a model with logs → feed prod usage back for the next version. Minutes of attention, no surprise bill, and a number she can trust because it came from data the optimizer never saw.
