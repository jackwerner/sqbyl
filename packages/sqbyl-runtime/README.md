# sqbyl-runtime

The minimal, dependency-light runtime for [sqbyl](https://github.com/jackwerner/sqbyl).

Embed a released sqbyl agent into your production app with three lines:

```python
from sqbyl_runtime import load

agent = load("revenue-analytics.v1.json", db=env.DATABASE_URL, model="claude-opus-4-8")
agent.ask("net revenue last month")  # → {plan, sql, rows, used_assets, usage, latency}
```

This package contains **only** release `load()` + `ask()` + structured logging.
None of the dev toolkit (eval, synth, Coach, judges, review console) ships here —
that all lives in the full `sqbyl` package. The one-way dependency arrow
(`sqbyl` → `sqbyl-runtime`, never the reverse) is enforced in CI.
