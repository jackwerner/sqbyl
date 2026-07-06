# Configuration reference

Project configuration lives in `sqbyl.yaml`. Secrets are referenced by `env:` name, never
inlined. A minimal manifest:

```yaml
name: revenue-analytics
database:
  dialect: postgresql        # postgresql | duckdb | snowflake | bigquery | mysql | sqlite
  url: env:DATABASE_URL
  read_only: true            # refuses non-SELECT; warns if the credential can write
model:
  provider: anthropic         # anthropic | openai — one provider powers every role
  api_key: env:ANTHROPIC_API_KEY
  default: claude-opus-4-8
```

To use OpenAI instead, switch the three provider lines — everything else is identical:

```yaml
model:
  provider: openai
  api_key: env:OPENAI_API_KEY
  default: gpt-5
```

sqbyl uses a **single provider for everything** — agent, judges, and Coach — so you pick one
and it applies across the loop (no mixing).

## `database`

| Key | Type | Default | Notes |
|---|---|---|---|
| `dialect` | enum | *required* | `postgresql`, `duckdb`, `snowflake`, `bigquery`, `mysql`, `sqlite`. |
| `url` | string | *required* | Connection URL; prefer `env:DATABASE_URL` indirection. |
| `read_only` | bool | `true` | Refuse non-SELECT; warn if the credential can write. |

## `model`

One provider + key, many roles. Each role's model is independently pinnable; unset roles
fall back to `default`.

| Key | Type | Default | Notes |
|---|---|---|---|
| `provider` | enum | `anthropic` | `anthropic` or `openai`. Used for every role. |
| `api_key` | string | *required* | Prefer `env:` indirection (`env:ANTHROPIC_API_KEY` / `env:OPENAI_API_KEY`). |
| `base_url` | string | `null` | Alternate provider endpoint (corporate proxy / AI gateway). Plain URL or `env:VAR`. |
| `default` | string | `claude-opus-4-8` | Model used by any role without its own pin. |
| `agent_model` | string | *(default)* | The response model. |
| `judge_model` | string | *(default)* | The advisory LLM judge. |
| `selection_model` | string | *(default)* | Large-schema context selection. |
| `orchestrator_model` | string | *(default)* | The orchestrator. |
| `synth_model` | string | *(default)* | Benchmark synthesis. |
| `coach_model` | string | *(default)* | The Coach. |

### Per-role model pinning

Out of the box the agent (response) and the judge are the **same** model. You can pin them
apart:

```yaml
model:
  default: claude-opus-4-8
  agent_model: claude-opus-4-8      # the response model
  judge_model: claude-sonnet-5      # a cheaper/independent judge
```

!!! tip "Judge independence"
    Using the *same* model as agent and judge risks correlated blind spots — the judge
    shares the agent's failure modes. Where independence matters, pin a **different**
    `judge_model`. The headline accuracy is deterministic regardless, so this only affects
    the advisory judge layer, and the scorecard stamps the model per role for provenance.

### Routing through a gateway

To route through a corporate proxy or AI gateway, set `model.base_url` (or pass `base_url=`
to the runtime `load()`) — no other change needed.

## `automation`

| Key | Type | Default | Notes |
|---|---|---|---|
| `auto_judge` | bool | `true` | Run the advisory judge automatically after an eval. |
| `auto_coach` | bool | `true` | Run the Coach automatically after an eval. |

When off, sqbyl still prints a one-line nudge after each run so the capability stays
discoverable.

## `defaults`

| Key | Type | Default | Notes |
|---|---|---|---|
| `max_tables_warn` | int | `7` | "Small space" nudge threshold. |
| `self_repair_attempts` | int | `2` | Static-validate → self-repair retries. |
| `prompt_caching` | bool | `true` | Cache the stable prompt prefix where the provider supports it. |
| `readiness_target` | float | `0.95` | Accuracy the readiness meter counts down to. |
| `auto_apply_threshold` | float | `0.85` | Machine decisions at/above this confidence auto-apply, with one-click undo. Set to `1.0` to require a human on everything. |

## `selection`

How the context compiler narrows tables/examples for a question. Defaults to include-all
(the small-project posture); set a strategy for large schemas.

| Key | Type | Default | Notes |
|---|---|---|---|
| `strategy` | enum | `include_all` | `include_all`, `lexical`, `llm`, `llm_lexical`. |
| `max_tables` | int | `null` | Above this count, "include everything" stops being viable. |
| `value_matching` | bool | `false` | Lexically match high-cardinality terms to declared sample values (`"EMEA"` → `region='emea'`). |
