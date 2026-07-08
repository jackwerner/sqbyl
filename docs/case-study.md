---
title: "Case study: 70% to 96% text-to-SQL accuracy"
description: >-
  One small LLM (Claude Haiku 4.5) went from 70.2% to 96.5% accuracy on business
  questions — held-out set included — by putting sqbyl's governed semantic layer between
  the model and a Postgres database. Same model, same data, honest numbers.
---

# From 70% to 96%: production-grade text-to-SQL, set up in minutes

## The result

We took one LLM — Anthropic's **Claude Haiku 4.5**, deliberately a small, fast, low-cost
model — pointed it at a Postgres database, and asked it the same 57 business questions
twice: once with nothing but the raw database schema, once behind sqbyl.

| | Raw LLM on the schema | Same LLM, with sqbyl |
|---|---|---|
| Dev questions (42) | 69.0% correct | **97.6% correct** |
| Held-out questions (15) | 73.3% correct | **93.3% correct** |
| **Overall** | **70.2%** | **96.5%** |

Same model. Same database. Same questions. What changed is that sqbyl built and governed
the semantic layer the model reasons over — automatically, but with a human in control.

That the model here is Haiku 4.5 *strengthens* the result on both axes that matter to an
enterprise buyer: the accuracy lift comes without reaching for a bigger, slower, more
expensive model, and the per-query economics (below) are those of the cheapest tier.

## The problem with the two usual options

Every team that wants reliable text-to-SQL hits the same wall: a raw LLM doesn't know what
your columns *mean*. `in_stock` — is that a unit count or a boolean? `cost_price` vs.
`unit_price` — which one does a user mean by "cost"? Get it wrong and you don't get an
error, you get a confident, wrong answer. That's the 30-point gap in the table above.

Teams typically close it one of two ways, and both have a catch:

- **Hand-build the semantic layer** — accurate, but slow, expensive to maintain, and
  perpetually stale. Someone has to write and re-write a description for every column on
  every schema change, forever.
- **Point a black-box auto-generator at the schema** — fast, but unaccountable. You can't
  easily see what it guessed, you can't approve it before it ships, and — the part that
  quietly poisons every accuracy number in this category — nothing stops it from tuning on
  and grading against its *own* test set. The "accuracy" it reports back is inflated by
  construction.

## How sqbyl does it — automatic, but governed

sqbyl's whole design premise is that you shouldn't have to choose between *automatic* and
*trustworthy*. In this case study, one command — `sqbyl init` — did all of the following,
and every step is built to keep a human in control and the numbers honest:

**1. It drafted the entire semantic layer for us — we wrote none of it.**
sqbyl automatically profiled all 6 tables and drafted plain-English descriptions and
business synonyms for all 35 columns, grounded in the real data distribution it measured.
Zero hand-authored documentation. This is the "minimal intervention" half.

**2. It never spent a cent without showing us the bill first.**
Before any paid work, sqbyl presents a line-itemed cost estimate and waits for approval —
you can approve, pick a subset of steps, swap to a cheaper model, or walk away. No
surprise spend, no black-box billing. Setup here came to about **two cents**.

**3. It flagged its own uncertainty instead of guessing silently.**
When sqbyl detected that "cost" could describe *either* `cost_price` or `unit_price`, it
didn't just pick one and move on. It **capped its confidence on the contested columns
below the auto-apply threshold and routed them to a human** for a decision — exactly the
place a wrong guess would have cost you a silently-wrong answer in production. High-
confidence annotations apply automatically; ambiguous ones surface for review. That's the
"human approval" half, and it's a UX principle, not an afterthought.

**4. It measured itself honestly — and refused to grade on a curve.**
The 93.3% held-out number is real. sqbyl keeps a strict wall between the questions it
tunes on and the held-out set it reports against — the held-out set is never
auto-generated and never trained on. Even sqbyl's optional "learn from a held-out failure"
path is walled off from the answer key by construction and quarantines any item a human
inspected, so a fixed question can never quietly inflate the headline score. When we ran
sqbyl's optimizer against a 95% target, it confirmed the project already cleared the bar
and stopped — it didn't manufacture busywork to look busy.

The through-line: **sqbyl automates the labor and keeps the judgment with you.** You get
the speed of auto-generation and the accountability of a hand-built layer, without picking
one.

**And it happened in minutes.** Building and governing the entire semantic layer — profile,
draft, flag ambiguities, and score a 42-question baseline — was a single `sqbyl init` pass
that ran in a few minutes, versus the days or weeks a hand-built layer takes. Each
before/after accuracy measurement was another minute or two. And there was headroom we
didn't spend: when we pointed sqbyl's optimizer at a 95% target, it confirmed we'd
*already* cleared it and stopped rather than burn budget for show. These are a few-minutes,
first-pass result — the ceiling is higher, we just didn't need to reach for it.

## What that looks like on a real question

**"How many products are currently in stock?"**
The raw LLM didn't know what `in_stock` was and guessed — it *summed* the column, answering
"how many units are in stock" instead of "how many products have stock." Confidently wrong.
sqbyl had already auto-annotated `in_stock` as "the current quantity of the product
available in inventory," so the governed agent counted products with stock — correctly,
on every run. Nobody on our side wrote that description; sqbyl drafted it and we approved
the pass.

**"Products that cost more than $100" — the honest hard case.**
This is the one sqbyl flagged as ambiguous up front. "Cost" genuinely maps to two columns,
and rather than hide that, sqbyl surfaced it for a human call. That's the difference
between a tool that's confidently wrong and one that tells you where it's unsure — which,
for anyone putting these answers in front of executives, is the entire ballgame.

## Unit economics: what it costs to run

Setup is one-time. What matters for a production deployment is the run-rate — and because
the whole result rides on Claude Haiku 4.5, that run-rate is the cheapest tier available.
These are sqbyl's own metered operational numbers, not estimates:

| Metric | Value |
|---|---|
| Model | Claude Haiku 4.5 |
| Cost per query | **$0.0042** |
| Tokens per query | 3,497 |
| p50 latency | 1.95s |
| p95 latency | 3.49s |

Scaled to volume:

| Query volume / month | Projected monthly cost |
|---|---|
| 10,000 | **$42** |
| 100,000 | **$423** |
| 1,000,000 | **$4,227** |

Under half a cent per question, answering at 96.5% accuracy against a live database — on a
semantic layer that cost two cents and minutes to build, and that a human signed off on.

## From blessed to deployed: a production endpoint in ~20 lines

The accuracy and economics above aren't a lab artifact you then have to go re-engineer for
production. Once a version clears the bar, `sqbyl release` compiles it into a single
portable file stamped with its held-out scorecard — and a lightweight runtime, sqbyl's own
`sqbyl-runtime` package (no dev tooling, no eval machinery), loads that file and serves it.
Dropping it behind FastAPI is about twenty lines:

```python
import os, sqbyl_runtime
from fastapi import FastAPI
from pydantic import BaseModel

agent = sqbyl_runtime.load(
    "sqbyl-casestudy-v2.v1.json",         # the blessed release + its scorecard
    db=os.environ["DATABASE_URL"],
    model="claude-haiku-4-5-20251001",
    narrate=True,                          # opt-in: also return a plain-English answer
)

app = FastAPI()

class Ask(BaseModel):
    question: str

@app.post("/ask")
def ask(body: Ask):
    r = agent.ask(body.question)
    return {"answer": r.answer, "sql": r.sql, "columns": r.columns, "rows": r.rows}
```

That's the whole integration. We actually stood this up and sent it live questions:

```
POST /ask   {"question": "Which product category has generated the most revenue?"}
→ "answer": "The Home & Kitchen product category has generated the most revenue
             with $1,756,898.34."
   "rows":  [["Home & Kitchen", 1756898.34]]

POST /ask   {"question": "How many customers have churned?"}
→ "answer": "21 customers have churned."
   "sql":   SELECT COUNT(*) ... FROM customers WHERE is_active = false
```

Two things worth calling out for an engineering evaluator:

- **The plain-English `answer` is opt-in and grounded, not a black box.** That `narrate`
  flag adds one extra summarization step *over the already-executed rows* — the SQL and the
  returned rows remain the authoritative result, and the runtime never reports a figure in
  the sentence that isn't in the data. Off by default, so the deterministic path stays the
  default. Leave it off for a BI backend that renders its own tables; turn it on for a chat
  or Slack surface that needs a sentence.
- **The runtime is decoupled from the toolkit.** You build, evaluate, and bless with the
  full sqbyl toolchain; you *deploy* the compact `sqbyl-runtime` with just the release file.
  The same release also serves over MCP or as a built-in chat server (`sqbyl run`) if you'd
  rather not write the FastAPI wrapper at all — and it can route through a corporate LLM
  gateway or proxy with a one-line `base_url` change.

## Bottom line

A raw LLM on your schema gets business questions right about 7 times out of 10. The same
model — a small, cheap one — behind sqbyl gets them right better than 19 times out of 20,
from a first-pass setup that ran in **minutes, not months**. sqbyl gets you there by
automating the tedious part (drafting the semantic layer) while keeping you in control of
the part that matters (approving it, and measuring it honestly). Fast like auto-generation,
accountable like a hand-built layer, and priced like the smallest model on the menu — with
room to climb higher whenever you want it. And when it's blessed, it ships behind a
production endpoint in twenty lines.

---

!!! note "Methodology"
    Model: Claude Haiku 4.5. Accuracy was verified by executing both the agent's SQL and
    the reference SQL against the live database and comparing results. Held-out figures are
    from a hand-authored question set never used for tuning. Cost and latency figures are
    sqbyl's own metered operational report. The deployment snippet was run live against the
    blessed release; the responses shown are actual endpoint output.
