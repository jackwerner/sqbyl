<div class="sqbyl-hero" markdown>
![sqbyl](assets/sqbyl-logo.png)
</div>

**An open-source, LLM-powered toolkit for building, evaluating, and iterating on
text-to-SQL agents over your own database.**

Bring your own database and one LLM provider key (Anthropic or OpenAI). sqbyl uses your
chosen model to both *answer* natural-language questions against your data **and** *coach
you* on how to make the agent answer them better — then ships the result as a single
portable file you can drop into production.

```bash
sqbyl init                      # connect, profile, annotate → a working agent
sqbyl eval dev                  # measure on your iteration set
sqbyl coach                     # ranked, applyable fixes for whatever failed
sqbyl coach apply 1 2           # apply them — git tracks every diff
sqbyl eval test                 # the honest, held-out accuracy number
sqbyl release create --tag v1   # ship it as one portable JSON
```

<figure markdown>
  ![sqbyl answering a natural-language question with SQL and rows](assets/demo.gif)
  <figcaption>One question → plan, SQL, and rows on the bundled DuckDB project.</figcaption>
</figure>

## Why sqbyl

If you want a trustworthy natural-language-to-SQL surface over a plain
Postgres/DuckDB/Snowflake warehouse, your options are roughly: pay for a closed platform
that locks the semantic layer, the judges, and the optimizer inside a walled garden — or
wire up a library yourself and hand-author all the metadata, evals, and prompt tuning.

sqbyl is the middle path. It reproduces the **build → evaluate → get told how to improve →
re-evaluate** loop as plain files in a git repo — and it's built so the accuracy number
that loop produces is one you can actually **report to stakeholders and defend**.

- **No black box.** Every prompt, judge, and improvement proposal is readable, editable
  plain text/JSON.
- **No second vendor.** A single provider key (Anthropic or OpenAI) powers the agent, the
  judges, and the Coach. Context selection is LLM/lexical, so there's no embeddings
  provider or vector store to run.
- **No surprise bill.** The free, deterministic work (connect, profile, infer joins) runs
  first at $0. Paid work is estimated up front, metered live, and capped by `--budget`.
- **Versioned like code.** Your whole "agent" is a directory of YAML you diff, review, and
  `git revert`.
- **Defensible by design.** The headline accuracy is deterministic and measured on a
  *held-out* set the improvement loop can never touch — so "we hit 94%" is a claim that
  survives scrutiny, not a benchmark you overfit.
  [Read why →](concepts.md#defensible-measurement)

## Where to next

<div class="grid cards" markdown>

- :material-rocket-launch: **[Getting started](getting-started.md)** — install, connect a
  database, run the guided setup, and ship your first release.
- :material-lightbulb-on: **[Concepts](concepts.md)** — the loop, dev/test discipline,
  defensible measurement, and the two-package architecture.
- :material-server: **[Embedding the runtime](guides/embedding.md)** — put a release behind
  your own API, with the async/concurrency rules.
- :material-cog: **[Configuration](guides/configuration.md)** — the `sqbyl.yaml` manifest,
  provider selection, and per-role model pinning.
- :material-console: **[CLI reference](cli.md)** — every command and what it does.
- :material-book-open-variant: **[Design spec](sqbyl-design-spec.md)** — the full *why*
  behind the product.

</div>
