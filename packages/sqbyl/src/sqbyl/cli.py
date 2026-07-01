"""Thin CLI wrapper over the Python API.

The Python API (``sqbyl.Project`` + the introspect/profile functions) is the
substrate; this CLI is a thin shell over it. Commands are added phase by phase.

Phase 1 surfaces the free, deterministic "$0" pass (spec §5.5): ``introspect`` and
``profile`` cost no tokens and run no LLM, so they print that framing.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from sqbyl import __version__

if TYPE_CHECKING:
    from sqbyl.project import Project
    from sqbyl_runtime.llm.base import Usage


def _schema_export(args: list[str]) -> int:
    """`sqbyl schema export` — regenerate the checked-in release JSON Schema."""
    from sqbyl_runtime.schema import schema_text, write_release_schema

    if "--stdout" in args:
        sys.stdout.write(schema_text())
        return 0
    path = write_release_schema()
    print(f"wrote {path}")
    return 0


def _introspect(args: list[str]) -> int:
    """`sqbyl introspect [DIR] [--force]` — draft semantics/*.yaml from the live schema."""
    from sqbyl.introspect import introspect
    from sqbyl.project import Project
    from sqbyl.semantics_io import table_filename, write_draft

    force = "--force" in args
    positional = [a for a in args if not a.startswith("-")]
    project = Project.load(positional[0] if positional else ".")

    print("▸ introspecting schema (read-only SQL)…  ($0 — no LLM)")
    project.semantics_dir.mkdir(parents=True, exist_ok=True)
    with project.connect() as db:
        tables = introspect(db)

    wrote, skipped = 0, 0
    for table in tables:
        path = project.semantics_dir / table_filename(table.table)
        if path.exists() and not force:
            print(f"  · {path.name} exists — skipping (use --force to overwrite)")
            skipped += 1
            continue
        write_draft(table, path)
        print(f"  ✓ {path.name}  ({len(table.columns)} columns, {len(table.joins)} joins)")
        wrote += 1
    print(f"done — wrote {wrote}, skipped {skipped}")
    return 0


def _profile(args: list[str]) -> int:
    """`sqbyl profile [DIR]` — write deterministic profile: blocks into the semantics."""
    from sqbyl.profile import profile_table
    from sqbyl.project import Project
    from sqbyl.semantics_io import dump_yaml_path, load_for_profiling, merge_profiles

    positional = [a for a in args if not a.startswith("-")]
    project = Project.load(positional[0] if positional else ".")
    paths = sorted(project.semantics_dir.glob("*.yaml"))
    if not paths:
        print("no semantics/*.yaml found — run `sqbyl introspect` first")
        return 1

    print("▸ profiling columns (read-only SQL)…  ($0 — no LLM)")
    with project.connect() as db:
        for path in paths:
            loaded = load_for_profiling(path)
            if loaded.table_skipped:
                print(f"  · {path.name} opted out (profile: false) — skipping")
                continue
            profiled = profile_table(db, loaded.table, options=loaded.options)
            merged = merge_profiles(loaded, profiled)
            dump_yaml_path(merged, path)
            sampled = any(c.profile and c.profile.sampled for c in profiled.columns)
            note = " (sampled)" if sampled else ""
            print(f"  ✓ {path.name}{note}")
    print("done")
    return 0


def _opt(args: list[str], name: str) -> str | None:
    """Extract a ``--name VALUE`` option, or None if absent."""
    flag = f"--{name}"
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def _meter(
    project: Project, usage: Usage, *, model: str, command: str, role: str, run_id: str
) -> float:
    """Record a metered call to ``.sqbyl/usage.db`` and return its dollar cost."""
    from sqbyl_runtime.cost import price_usage
    from sqbyl_runtime.state.layout import SqbylPaths
    from sqbyl_runtime.state.usage import UsageRecord, UsageStore

    cost = price_usage(usage, model)
    paths = SqbylPaths(project.root).ensure()
    with UsageStore(paths.usage_db) as store:
        store.record(
            UsageRecord.from_usage(
                usage, model=model, command=command, role=role, cost_usd=cost, run_id=run_id
            )
        )
    return cost


def _ask(args: list[str]) -> int:
    """`sqbyl ask "question" [DIR] [--replay PATH]` — answer one question end-to-end."""
    from sqbyl.llm import build_llm_client
    from sqbyl.project import Project
    from sqbyl.projectfiles import load_knowledge
    from sqbyl_runtime.cost import estimate_cost
    from sqbyl_runtime.pipeline import ask
    from sqbyl_runtime.state.layout import SqbylPaths
    from sqbyl_runtime.state.traces import TraceWriter

    replay = _opt(args, "replay")
    positional = [a for a in args if not a.startswith("-") and a != replay]
    if not positional:
        print('usage: sqbyl ask "your question" [DIR] [--replay cassette.json]')
        return 2
    question = positional[0]
    project = Project.load(positional[1] if len(positional) > 1 else ".")
    model = project.manifest.model.for_role("agent")

    est = estimate_cost(model=model, calls=1, avg_input_tokens=1500, avg_output_tokens=300)
    print(f"▸ asking on {model} — estimated ~${est:.4f} for 1 call (paid)")

    knowledge = load_knowledge(project)
    llm = build_llm_client(project.manifest, replay=replay)
    paths = SqbylPaths(project.root).ensure()
    with project.connect() as db:
        result = ask(
            question,
            knowledge=knowledge,
            db=db,
            llm=llm,
            model=model,
            self_repair_attempts=project.manifest.defaults.self_repair_attempts,
            trace_writer=TraceWriter(paths.traces_dir / "ask.jsonl"),
        )
    cost = _meter(
        project, result.usage, model=model, command="ask", role="agent", run_id=result.trace_id
    )

    print(f"\nplan: {result.plan}")
    print(f"\nsql:\n{result.sql}")
    if result.ok:
        print(f"\nrows ({len(result.rows)}):")
        print("  " + " | ".join(result.columns))
        for row in result.rows[:20]:
            print("  " + " | ".join(str(v) for v in row))
        if result.used_assets:
            print(f"\ncited trusted assets: {', '.join(result.used_assets)}")
    else:
        print(f"\n✗ failed after {result.attempts} attempt(s): {result.error}")
    print(
        f"\nusage: {result.usage.total_tokens} tokens · ${cost:.4f} · "
        f"{result.attempts} attempt(s) · {result.latency_ms:.0f}ms"
    )
    return 0 if result.ok else 1


def _eval(args: list[str]) -> int:
    """`sqbyl eval [dev|test] [DIR] [--replay P] [--record P] [--as-of ISO]`.

    Runs each question as a fresh, stateless ``ask()``, scores it with the Layer-1
    deterministic scorers, prints per-run aggregates (accuracy with a 95% interval /
    manual-review / cost / latency), and — when an earlier run of the same split exists —
    the flipped-questions diff (regression detection, spec §7). Dev and test are always
    reported separately. ``--as-of`` pins the clock for ``now()``-relative gold so runs
    are reproducible across time.
    """
    from datetime import datetime

    from sqbyl.eval.benchmarks_io import Split
    from sqbyl.eval.heldout import load_for_eval
    from sqbyl.eval.report import diff_runs, load_runs, previous_run
    from sqbyl.project import Project
    from sqbyl_runtime.cost import estimate_cost
    from sqbyl_runtime.state.layout import SqbylPaths

    replay, record, as_of_opt = _opt(args, "replay"), _opt(args, "record"), _opt(args, "as-of")
    consumed = {replay, record, as_of_opt}
    positional = [a for a in args if not a.startswith("-") and a not in consumed]
    split_arg = "dev"
    if positional and positional[0] in ("dev", "test"):
        split_arg = positional.pop(0)
    project = Project.load(positional[0] if positional else ".")
    try:
        split = Split(split_arg)
    except ValueError:
        print(f"unknown split {split_arg!r}; expected 'dev' or 'test'")
        return 2
    try:
        as_of = datetime.fromisoformat(as_of_opt) if as_of_opt else None
    except ValueError:
        print(f"invalid --as-of {as_of_opt!r}; expected an ISO datetime like 2026-06-30")
        return 2

    model = project.manifest.model.for_role("agent")
    questions = load_for_eval(project, split)
    if not questions:
        print(f"benchmarks/{split.value}.yaml has no questions — run `sqbyl synth` first")
        return 1

    paths = SqbylPaths(project.root)
    if split is Split.test:
        # The held-out set is the honest, headline number; scoring it repeatedly while
        # iterating leaks it (spec §7). Nudge, and surface how often it's been scored.
        prior_test = len(load_runs(paths, split="test"))
        if prior_test:
            print(
                f"⚠ held-out test scored {prior_test} time(s) before — score it sparingly "
                "(ideally once per blessed version); iterate on dev."
            )

    est = estimate_cost(
        model=model, calls=len(questions), avg_input_tokens=1500, avg_output_tokens=300
    )
    print(
        f"▸ eval {split.value} — {len(questions)} question(s) on {model}, "
        f"estimated ~${est:.4f} (paid)"
    )

    run = project.eval(split.value, replay=replay, record=record, as_of=as_of)

    lo, hi = run.accuracy_ci()
    print(
        f"\naccuracy: {run.n_correct}/{run.total} ({run.accuracy:.1%}"
        f", 95% CI {lo:.0%}–{hi:.0%})"
        f" · manual review: {run.n_manual_review} · errors: {run.n_error}"
    )
    print(
        f"self-repair: {run.self_repair_rate:.1%} · mean latency: {run.mean_latency_ms:.0f}ms"
        f" · {run.total_tokens} tokens · ${run.total_cost_usd:.4f}"
    )
    print("models: " + ", ".join(f"{role}={m}" for role, m in sorted(run.models.items())))
    for r in run.results:
        mark = {"correct": "✓", "manual_review": "?", "error": "✗"}[r.verdict.value]
        print(f"  {mark} {r.id}  [{r.verdict.value}]")

    prev = previous_run(paths, run)
    if prev is not None:
        d = diff_runs(prev, run)
        if d.fixed or d.regressed:
            print(f"\nvs previous {split.value} run ({prev.run_id[:8]}):")
            if d.fixed:
                print(f"  fixed:     {', '.join(d.fixed)}")
            if d.regressed:
                print(f"  regressed: {', '.join(d.regressed)}")
        else:
            print(f"\nvs previous {split.value} run ({prev.run_id[:8]}): no questions flipped")
    return 0


def _synth(args: list[str]) -> int:
    """`sqbyl synth [DIR] [--n N] [--budget $N] [--replay P] [--record P] [--as-of ISO]`.

    Execution-grounded candidate generation (spec §6.A): one paid drafting call proposes
    ~N questions with gold SQL, then every gold query is **executed** and only the ones
    that actually run and return a real answer survive. Survivors land in the review queue
    (``.sqbyl/candidates.yaml``) for ``sqbyl review`` — never in the held-out ``test.yaml``.
    """
    from datetime import datetime

    from sqbyl.candidates_io import add_candidates, candidates_path
    from sqbyl.llm import build_llm_client
    from sqbyl.models import DropReason
    from sqbyl.project import Project
    from sqbyl.synth import synthesize
    from sqbyl_runtime.cost import estimate_cost
    from sqbyl_runtime.state.layout import SqbylPaths
    from sqbyl_runtime.state.traces import TraceWriter, new_trace_id

    replay, record, model_opt = _opt(args, "replay"), _opt(args, "record"), _opt(args, "model")
    n_opt, budget_opt, as_of_opt = _opt(args, "n"), _opt(args, "budget"), _opt(args, "as-of")
    consumed = {replay, record, model_opt, n_opt, budget_opt, as_of_opt}
    positional = [a for a in args if not a.startswith("-") and a not in consumed]
    project = Project.load(positional[0] if positional else ".")
    model = model_opt or project.manifest.model.for_role("synth")
    n = int(n_opt) if n_opt else 20
    budget = float(budget_opt) if budget_opt is not None else None
    try:
        as_of = datetime.fromisoformat(as_of_opt) if as_of_opt else None
    except ValueError:
        print(f"invalid --as-of {as_of_opt!r}; expected an ISO datetime like 2026-06-30")
        return 2

    # One drafting call producing a batch — estimate a large output for ~n questions.
    est = estimate_cost(model=model, calls=1, avg_input_tokens=2000, avg_output_tokens=2000)
    cap = f" · budget ${budget:.2f}" if budget is not None else ""
    print(f"▸ synth ~{n} candidate(s) on {model} — estimated ~${est:.4f} (paid){cap}")
    if budget is not None and est > budget:
        print(
            f"  ✗ estimate ~${est:.4f} exceeds budget ${budget:.2f} — raise --budget or lower --n"
        )
        return 1

    llm = build_llm_client(project.manifest, replay=replay, record=record)
    paths = SqbylPaths(project.root).ensure()
    result = synthesize(
        project,
        llm=llm,
        model=model,
        n=n,
        as_of=as_of,
        trace_writer=TraceWriter(paths.traces_dir / "synth.jsonl"),
    )
    spent = _meter(
        project, result.usage, model=model, command="synth", role="synth", run_id=new_trace_id()
    )
    add_candidates(project, result.survivors)

    print(
        f"\ndrafted {result.n_drafted} · kept {result.n_survivors} · "
        f"dropped {result.n_dropped} (execution-grounded) · ${spent:.4f}"
    )
    if result.dropped:
        by_reason: dict[str, int] = {}
        for d in result.dropped:
            by_reason[d.reason.value] = by_reason.get(d.reason.value, 0) + 1
        order = [r.value for r in DropReason]
        pretty = ", ".join(f"{k}={by_reason[k]}" for k in order if k in by_reason)
        print(f"  drops: {pretty}")
    if result.survivors:
        # Surface the survivor difficulty mix: execution-grounding drops empty results, so
        # the kept set skews toward satisfiable (often easier) questions — make that visible
        # rather than letting a lopsided dev set quietly imply broad coverage (spec §7).
        by_difficulty: dict[str, int] = {}
        for c in result.survivors:
            by_difficulty[c.difficulty or "—"] = by_difficulty.get(c.difficulty or "—", 0) + 1
        mix = ", ".join(f"{k}={by_difficulty[k]}" for k in sorted(by_difficulty))
        print(f"  difficulty mix: {mix}")
    for c in result.survivors:
        tag = "" if c.canonical else " (variant)"
        print(f"  ✓ {c.id}  [{c.difficulty or '—'}]{tag}  {c.evidence.row_count} row(s)")
    queue_name = candidates_path(project).name
    print(f"\n{result.n_survivors} candidate(s) queued → run `sqbyl review` ({queue_name})")
    return 0


def _review(args: list[str]) -> int:
    """`sqbyl review [DIR] [--host H] [--port N]` — launch the local review console."""
    import uvicorn

    from sqbyl.console import create_app
    from sqbyl.project import Project

    host_opt, port_opt = _opt(args, "host"), _opt(args, "port")
    consumed = {host_opt, port_opt}
    positional = [a for a in args if not a.startswith("-") and a not in consumed]
    project = Project.load(positional[0] if positional else ".")
    host, port = host_opt or "127.0.0.1", int(port_opt) if port_opt else 8765

    app = create_app(project)
    print(f"▸ sqbyl review — golden-set console at http://{host}:{port}  (Ctrl-C to stop)  ($0)")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


def _annotate(args: list[str]) -> int:
    """`sqbyl annotate [DIR] [--replay P] [--record P] [--model M] [--budget $N]`.

    Drafts descriptions for every table. As a paid, multi-call command it caps spend
    at ``--budget`` (the full guided/--auto budget machinery is Phase 7): once the
    metered total reaches the cap, remaining tables are left for a later run.
    """
    from sqbyl.annotate import annotate_table
    from sqbyl.llm import build_llm_client
    from sqbyl.project import Project
    from sqbyl.semantics_io import dump_yaml_path, merge_annotation
    from sqbyl.yamlio import load_yaml
    from sqbyl_runtime.cost import estimate_cost
    from sqbyl_runtime.models import TableSemantics
    from sqbyl_runtime.state.layout import SqbylPaths
    from sqbyl_runtime.state.traces import Span, TraceWriter, new_span_id, new_trace_id

    replay, record, model_opt = _opt(args, "replay"), _opt(args, "record"), _opt(args, "model")
    budget_opt = _opt(args, "budget")
    budget = float(budget_opt) if budget_opt is not None else None
    consumed = {replay, record, model_opt, budget_opt}
    positional = [a for a in args if not a.startswith("-") and a not in consumed]
    project = Project.load(positional[0] if positional else ".")
    model = model_opt or project.manifest.model.default

    paths = sorted(project.semantics_dir.glob("*.yaml"))
    if not paths:
        print("no semantics/*.yaml found — run `sqbyl introspect` and `sqbyl profile` first")
        return 1

    est = estimate_cost(model=model, calls=len(paths), avg_input_tokens=1500, avg_output_tokens=400)
    cap = f" · budget ${budget:.2f}" if budget is not None else ""
    print(f"▸ annotating {len(paths)} table(s) on {model} — estimated ~${est:.4f} (paid){cap}")

    llm = build_llm_client(project.manifest, replay=replay, record=record)
    state = SqbylPaths(project.root).ensure()
    trace_writer = TraceWriter(state.traces_dir / "annotate.jsonl")
    run_span = Span(
        name="annotate",
        trace_id=new_trace_id(),
        span_id=new_span_id(),
        attributes={"gen_ai.operation.name": "chat", "sqbyl.tables": len(paths)},
    )

    spent, done = 0.0, 0
    for path in paths:
        if budget is not None and spent >= budget:
            left = len(paths) - done
            print(f"  ⏸ budget ${budget:.2f} reached — {left} table(s) left; re-run to continue")
            break
        raw = load_yaml(path.read_text())
        table = TableSemantics.model_validate(raw)
        annotation, response = annotate_table(
            llm,
            table,
            model=model,
            trace_writer=trace_writer,
            trace_id=run_span.trace_id,
            parent_span_id=run_span.span_id,
        )
        dump_yaml_path(merge_annotation(raw, annotation), path)
        spent += _meter(
            project,
            response.usage,
            model=model,
            command="annotate",
            role="annotator",
            run_id=run_span.trace_id,
        )
        done += 1
        print(f"  ✓ {path.name}  (table confidence {annotation.confidence:.2f})")
    trace_writer.write(run_span.end(status="ok"))
    print(f"done — annotated {done}/{len(paths)}, metered ${spent:.4f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in {"-V", "--version", "version"}:
        print(f"sqbyl {__version__}")
        return 0
    if len(args) >= 2 and args[0] == "schema" and args[1] == "export":
        return _schema_export(args[2:])
    if args and args[0] == "introspect":
        return _introspect(args[1:])
    if args and args[0] == "profile":
        return _profile(args[1:])
    if args and args[0] == "annotate":
        return _annotate(args[1:])
    if args and args[0] == "ask":
        return _ask(args[1:])
    if args and args[0] == "eval":
        return _eval(args[1:])
    if args and args[0] == "synth":
        return _synth(args[1:])
    if args and args[0] == "review":
        return _review(args[1:])
    print(
        "sqbyl: commands — introspect, profile, annotate, ask, eval, synth, review, "
        "schema export, version"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
