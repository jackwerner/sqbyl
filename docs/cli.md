# CLI reference

The core command surface:

```
sqbyl init [<db-url>]     # guided: free profile → costed plan → confirm → step through
                          #   scaffolds sqbyl.yaml if missing; --auto --budget $5 for CI;
                          #   --dry-run to estimate only; --model M reprices every role
sqbyl review              # attention queue + golden-set / judge / proposal review (web UI)
sqbyl eval [dev|test]     # run the eval harness → scored report + run diff
                          #   --trials N re-runs to expose hosted-model variance (reports the spread)
sqbyl eval show <split> <id>   # print one saved row's full detail (plan/SQL/scorers/judges), $0
sqbyl synth [--n 40]      # execution-grounded candidate questions → dev set
sqbyl coach [apply N... | --regenerate]   # review/apply context edits; reuses the last report ($0)
sqbyl coach --from-test-failure <id>      # diagnose ONE held-out failure from the agent's trace
                          #   (never its gold); human-review-only, provenance-stamped, item quarantined
sqbyl optimize --budget $5 --target 0.9   # autonomous coach→apply→eval loop on dev
                          #   --trials N scores each candidate N× (keep on a majority — variance guard);
                          #   --require-significant also gates keeps on a paired sign test
sqbyl ask "..."           # one-shot NL→SQL→result (--narrate adds a plain-English answer)
sqbyl release create --tag v1             # bless current version → portable JSON
sqbyl cost <command>      # estimate $ / tokens, spend nothing
sqbyl reset [--all]       # clear local .sqbyl/ state (keeps cost history unless --all)
```

`sqbyl init` **scaffolds `sqbyl.yaml` for you** when there isn't one — interactively on a
terminal, or as a ready-to-fill template under `--auto`. It also runs a `$0` credential check
(a token-free provider call) before quoting a plan, so a bad key fails fast, not mid-spend.

Per-step à-la-carte commands are documented in the [design spec, §10](sqbyl-design-spec.md):
`introspect` (add `--sync` to merge **new** live columns into existing semantics files without
losing annotations, or `--force` to redraft), `profile`, `annotate`, `judge`, `runs`, `serve`,
`run`.

A benchmark question defaults to **exact** result-set matching. Set `match_mode: columns_superset`
on a question to also count an answer correct when it reproduces every gold column and row but
adds *extra* informative columns — a deliberately weaker bar you opt into per question (an extra
ungrouped column can change an aggregate's meaning, so it is never the default).

## Cost & safety flags

Every paid command shares the same cost posture (see [Concepts → Cost posture](concepts.md#cost-posture)):

- `--budget $N` — cap spend. **Guided** runs pause and ask on approaching it; `--auto` runs
  hard-stop, and `--budget` is **required** under `--auto`.
- `--dry-run` — print the estimate and exit without spending.
- `sqbyl cost <command>` — estimate the tokens and dollars for any command, spending nothing.

## Serving a release

```
sqbyl run <release>       # invoke a release from the CLI (non-Python callers)
sqbyl serve               # quick local HTTP endpoint — dev only, not hardened
```

!!! warning
    `sqbyl serve` is a localhost dev convenience, **not** a production server. For
    production, [embed the runtime](guides/embedding.md) in your own service.
