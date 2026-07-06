# Embedding the runtime

Production is "just a model with logs." The dev machinery (eval, synth, coach, console) does
**not** come along — you embed the lightweight `sqbyl-runtime`:

```python
from sqbyl_runtime import load

agent = load("revenue-analytics.v1.json", db=env.DATABASE_URL, model="claude-opus-4-8")

@app.post("/ask")          # your API, your auth, your scaling
def ask(q: str):
    return agent.ask(q)    # → {plan, sql, rows, used_assets, usage, latency}
```

It inherits your app's auth, connection pooling, and observability. The model, key, and
database are injected at `load()` — not baked into the release.

!!! tip "Provider selection"
    `load()` takes a `provider` argument (default `"anthropic"`). Pass `provider="openai"`
    for an OpenAI-built release, and set the matching key (`ANTHROPIC_API_KEY` or
    `OPENAI_API_KEY`). To route through a corporate proxy or AI gateway, pass `base_url=`.

`sqbyl run <release>` / `sqbyl serve` exist for non-Python callers and quick HTTP exposure,
but are **intentionally not hardened** — don't put `sqbyl serve` on the open internet. The
sanctioned production path is embedding the runtime in your own service.

## Async & concurrency

`agent.ask()` is **synchronous and blocking** (an LLM round-trip plus DB queries), but a
single loaded `agent` is **safe to call concurrently** — the DB engine pools per-thread
connections, the provider client (Anthropic or OpenAI) is thread-safe, and trace writes are
locked. So under a threadpool it serves concurrent requests correctly.

The one rule for an **async** server: run `ask()` off the event loop; don't call it inside an
`async def` directly (that blocks the loop for the whole request).

```python
# FastAPI: a sync endpoint is auto-run in a threadpool — this is the example above, correct.
@app.post("/ask")
def ask(q: str): ...

# From an async endpoint, offload explicitly:
from starlette.concurrency import run_in_threadpool

@app.post("/ask")
async def ask(q: str):
    return await run_in_threadpool(agent.ask, q)   # or asyncio.to_thread(agent.ask, q)
```

Bound concurrency (threadpool + DB pool size) as you would for any blocking workload. A
native-async runtime (async provider client + async DB) isn't provided — the threadpool
pattern is the supported path.

## The release interface

A release is a single portable JSON — the agent's *brain* (semantics, instructions,
examples, judge prompts, scorecard), never rows. It carries a `schema_version`; the runtime
warns on model or schema mismatch at `load()`. A third party can read the release without
sqbyl installed at all.
