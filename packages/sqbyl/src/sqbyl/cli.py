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
    from sqbyl.init import EnrichmentResult, FreePass, InitPlan
    from sqbyl.models.optimize import OptimizeResult
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


def _budget_opts(args: list[str]) -> tuple[float | None, bool, bool] | None:
    """Parse ``--budget $N`` / ``--auto`` / ``--dry-run`` shared by every paid command.

    Returns ``(budget, auto, dry_run)`` or ``None`` when the combination is invalid (an
    error is printed). ``--budget`` is **required** in ``--auto`` (invariant 5, spec §9):
    the headless path hard-stops at the cap, so it must be given one it can't silently blow.
    """
    auto = "--auto" in args
    dry_run = "--dry-run" in args
    budget_opt = _opt(args, "budget")
    budget: float | None = None
    if budget_opt is not None:
        try:
            budget = float(budget_opt.lstrip("$"))
        except ValueError:
            print(f"invalid --budget {budget_opt!r}; expected a dollar amount like 5 or $5")
            return None
        if budget < 0:
            print("--budget must be non-negative")
            return None
    if auto and budget is None:
        print("--auto requires --budget $N (a headless run must have a hard cap to stop at)")
        return None
    return budget, auto, dry_run


def _authorize(
    meter: object, next_cost: float, *, auto: bool, label: str, prompt: object = None
) -> bool:
    """Gate the next paid step against the live meter's budget (spec §5.5, §9).

    Under budget → proceed silently. Over budget → hard-stop in ``--auto``, or pause and
    ask in guided mode. ``prompt`` defaults to :func:`input`, resolved at call time so a
    test can patch ``builtins.input``.
    """
    from sqbyl_runtime.cost import SpendMeter

    assert isinstance(meter, SpendMeter)
    if not meter.would_exceed(next_cost):
        return True
    if auto:
        print(
            f"  ✗ budget ${meter.budget:.2f} reached (${meter.spent:.4f} spent) — "
            f"{label} needs ~${next_cost:.4f}; stopping"
        )
        return False
    remaining = meter.remaining or 0.0
    ask_fn = prompt if prompt is not None else input
    assert callable(ask_fn)
    answer = ask_fn(
        f"  ⏸ {label} ~${next_cost:.4f} would exceed the ${meter.budget:.2f} budget "
        f"(${remaining:.4f} left). Proceed anyway? [y/N] "
    )
    return str(answer).strip().lower().startswith("y")


def _cost(args: list[str]) -> int:
    """`sqbyl cost <command> [DIR] [--n N]` — estimate a paid command, spend nothing (spec §9)."""
    from sqbyl.estimates import estimate_for_command
    from sqbyl.project import Project

    if not args:
        print("usage: sqbyl cost <ask|annotate|synth|eval|coach> [DIR]")
        return 2
    command = args[0]
    n_opt = _opt(args, "n")
    positional = [a for a in args[1:] if not a.startswith("-") and a != n_opt]
    project = Project.load(positional[0] if positional else ".")
    try:
        estimate = estimate_for_command(project, command, n=int(n_opt) if n_opt else 20)
    except KeyError:
        print(f"'{command}' is not a paid command — nothing to estimate")
        return 2
    print(f"▸ cost estimate for `sqbyl {command}` (no API calls — nothing spent):\n")
    print(estimate.render())
    print(
        f"\n{estimate.calls} planned call(s) · ~${estimate.total_usd:.4f} — "
        "run the command (without --dry-run) to spend"
    )
    return 0


def _report(args: list[str]) -> int:
    """`sqbyl report [DIR] [--json] [--volume N]` — roll up usage/runs/latency → KpiReport.

    A reporting view over data already captured (spec §7.5): no tokens, no DB query,
    aggregates only. Dev and held-out test are reported separately, never conflated.
    """
    from sqbyl.project import Project

    as_json = "--json" in args
    volume_opt = _opt(args, "volume")
    positional = [a for a in args if not a.startswith("-") and a != volume_opt]
    project = Project.load(positional[0] if positional else ".")
    volume = int(volume_opt) if volume_opt else None
    report = project.kpis(volume=volume)

    if as_json:
        print(report.model_dump_json(indent=2))
        return 0

    print("▸ sqbyl report — operational KPIs ($0, aggregates only)\n")
    ue = report.unit_economics
    print("unit economics (agent / production)")
    print(
        f"  $/query ${ue.cost_per_query_usd:.4f} · {ue.tokens_per_query:.0f} tokens/query · "
        f"cache hit {ue.cache_hit_rate:.0%}"
    )
    if ue.judge_cost_per_query_usd:
        print(
            f"  + ${ue.judge_cost_per_query_usd:.4f}/query dev-only judge overhead "
            "(not in production)"
        )
    if ue.projected_monthly_usd is not None and ue.volume_per_month is not None:
        print(
            f"  projected run-rate ${ue.projected_monthly_usd:,.2f}/mo "
            f"at {ue.volume_per_month:,} queries/mo"
        )
    for q in (report.dev_quality, report.test_quality):
        if q is None:
            continue
        caveat = " — small sample, directional" if q.low_confidence else ""
        print(f"\nquality — {q.split}")
        print(
            f"  accuracy {q.accuracy:.0%} (95% CI {q.accuracy_low:.0%}–{q.accuracy_high:.0%}, "
            f"n={q.n}){caveat}"
        )
        print(f"  manual-review {q.manual_review_rate:.0%} · self-repair {q.self_repair_rate:.0%}")
    if report.dev_test_gap is not None:
        print(f"  dev↔test gap {report.dev_test_gap:+.0%} (overfitting signal)")
        # The gap is only a valid overfitting signal if both splits ran on the same agent.
        dev_agent = (report.dev_quality.models if report.dev_quality else {}).get("agent")
        test_agent = (report.test_quality.models if report.test_quality else {}).get("agent")
        if dev_agent and test_agent and dev_agent != test_agent:
            print(
                f"  ⚠ dev ({dev_agent}) and test ({test_agent}) ran different agent models — "
                "the gap conflates a model change with overfitting; re-score on one model."
            )
    if report.performance is not None:
        p = report.performance
        perf_caveat = " — small sample, directional" if p.low_confidence else ""
        print(
            f"\nperformance\n  latency p50 {p.latency_p50_ms:.0f}ms · "
            f"p95 {p.latency_p95_ms:.0f}ms{perf_caveat}"
        )
    gap = report.readiness_gap
    status = ""
    if gap is not None:
        status = " · reached (CI clears target)" if report.readiness_met else f" · {gap:.0%} to go"
    print(
        f"\nprocess\n  readiness target {report.readiness_target:.0%}{status} · "
        f"{report.round_trips_to_ship} dev run(s) recorded"
    )
    print(
        f"\nlifetime (all roles + commands)  ${report.total_cost_usd:.4f} · "
        f"{report.total_tokens:,} tokens (from usage.db)"
    )
    return 0


def _release(args: list[str]) -> int:
    """`sqbyl release create --tag vN [DIR] [-o PATH]` — compile the project into a
    portable release JSON stamped with the held-out scorecard (spec §11, plan 8.1).

    A $0 file operation: no DB connection, no tokens. The headline accuracy is read from
    the most recent persisted **held-out test** run — run `sqbyl eval test` first."""
    from sqbyl.eval.judges import load_judge_prompts
    from sqbyl.project import Project
    from sqbyl.release import OVERFITTING_GAP, ReleaseError, build_release, overfitting_gap
    from sqbyl.release import write_release as _write

    if not args or args[0] != "create":
        print("usage: sqbyl release create --tag vN [DIR] [-o PATH]")
        return 2
    rest = args[1:]
    tag = _opt(rest, "tag")
    out = _opt(rest, "out") or _opt(rest, "o")
    if tag is None:
        print("release create needs --tag (e.g. --tag v1)")
        return 2
    consumed = {tag, out}
    positional = [a for a in rest if not a.startswith("-") and a not in consumed]
    project = Project.load(positional[0] if positional else ".")
    try:
        release = build_release(project, tag, judge_prompts=load_judge_prompts(project))
    except ReleaseError as exc:
        print(f"cannot build release: {exc}")
        return 1

    destination = out or str(project.root)
    path = _write(release, destination)
    sc = release.scorecard
    print(f"▸ release {release.name} {release.tag} → {path}")
    # Headline accuracy with its Wilson interval — a point estimate over tens of questions
    # is not a settled number (ml-systems). Small n gets an explicit caveat.
    ci = ""
    if sc.accuracy_low is not None and sc.accuracy_high is not None:
        ci = f", 95% CI {sc.accuracy_low:.0%}–{sc.accuracy_high:.0%}"
    unresolved = f", {sc.n_manual_review} unresolved" if sc.n_manual_review else ""
    dev = f" · dev {sc.dev_accuracy:.0%} (n={sc.dev_n})" if sc.dev_accuracy is not None else ""
    caveat = " — small sample, directional" if sc.n < _SMALL_EVAL else ""
    print(
        f"  headline accuracy {sc.accuracy:.0%} "
        f"(held-out test, n={sc.n}{ci}{unresolved}){dev}{caveat}"
    )
    if sc.judge_human_agreement is not None:
        print(
            f"  judge↔human agreement {sc.judge_human_agreement:.0%} "
            f"(n={sc.judge_human_agreement_n}, reviewed rows only)"
        )
    print(f"  blessed on {sc.blessed_with_models or '{}'} · schema {release.schema_fingerprint}")
    # `release` never connects (it's $0), so the schema fingerprint is a snapshot from the last
    # `eval test` — a DB schema change since then, with files unchanged, is invisible here and
    # only surfaces as a load-time warning. Nudge the user to eval immediately before releasing.
    print("  (schema fingerprint is from the last `eval test`; re-run it if the DB changed since)")
    if sc.knowledge_fingerprint is None:
        print(
            "  ⚠ scorecard provenance unverified — the held-out eval predates fingerprinting; "
            "re-run `sqbyl eval test` to tie the number to these exact files."
        )
    # A large dev↔test gap means the loop may have overfit dev rather than generalizing (§11).
    gap = overfitting_gap(release)
    if gap is not None and gap > OVERFITTING_GAP:
        small = sc.n < _SMALL_EVAL or (sc.dev_n or 0) < _SMALL_EVAL
        hedge = (
            " (both sets are small — read this as directional, not a settled gap)" if small else ""
        )
        print(
            f"  ⚠ dev↔test gap {gap:+.0%} exceeds {OVERFITTING_GAP:.0%} — the held-out number "
            f"is the one to trust; a large gap suggests overfitting, not generalization.{hedge}"
        )
    return 0


# Below this many questions an eval accuracy is directional, not a settled number — the same
# small-sample posture the §7.5 report takes; kept local to the release CLI's caveats.
_SMALL_EVAL = 30


def _optimize(args: list[str]) -> int:
    """`sqbyl optimize [DIR] --budget $N [--target T] [--max-rounds M] [--json] [--dry-run]`.

    The autonomous loop (spec §6.C): coach → apply → eval **on dev only**, keeping edits that
    raise the dev score and reverting those that don't, until ``--target`` or ``--budget`` is
    hit. Returns a frontier of versions (readable git diffs); the held-out test is scored
    **once** on the picked version, and a large dev↔test gap prints an overfitting warning.
    """
    from sqbyl.eval.benchmarks_io import dev_set_size
    from sqbyl.llm import build_llm_client
    from sqbyl.optimize import optimize
    from sqbyl.project import Project

    budget_parse = _budget_opts(args)
    if budget_parse is None:
        return 2
    budget, auto, dry_run = budget_parse
    target_opt, rounds_opt = _opt(args, "target"), _opt(args, "max-rounds")
    gain_opt = _opt(args, "min-gain")
    replay, record = _opt(args, "replay"), _opt(args, "record")
    as_json = "--json" in args
    consumed = {target_opt, rounds_opt, gain_opt, replay, record}
    positional = [a for a in args if not a.startswith("-") and a not in consumed]
    project = Project.load(positional[0] if positional else ".")

    # optimize is autonomous — it spends across many rounds without a human in the loop — so a
    # hard budget cap is required, not just under --auto (invariant 5).
    if budget is None and not dry_run:
        print("optimize requires --budget $N (it runs an autonomous, multi-round paid loop)")
        return 2
    target = float(target_opt) if target_opt else project.manifest.defaults.readiness_target
    max_rounds = int(rounds_opt) if rounds_opt else 10
    min_gain = int(gain_opt) if gain_opt else 1
    dev_n = dev_set_size(project)

    _print_optimize_estimate(project, dev_n, budget, dry_run=dry_run)
    if dry_run:
        return 0
    # An unattended, multi-round spend that MUTATES the working tree — get consent up front, not
    # just a cap (invariant 5 / responsible-ai): show what it'll do, then ask (guided) or note
    # the cap is the floor (--auto). Warn if the tree is dirty so machine edits stay separable.
    _warn_if_dirty(project)
    if not auto and not _confirm("proceed with the autonomous optimize run?"):
        print("cancelled — nothing spent.")
        return 0

    client = build_llm_client(project.manifest, replay=replay, record=record)
    result = optimize(
        project, llm=client, target=target, budget=budget, min_gain=min_gain, max_rounds=max_rounds
    )

    if as_json:
        print(result.model_dump_json(indent=2))
        return 0
    return _report_optimize(result)


def _print_optimize_estimate(
    project: Project, dev_n: int, budget: float | None, *, dry_run: bool
) -> None:
    from sqbyl.estimates import coach_estimate, eval_estimate

    agent_model = project.manifest.model.for_role("agent")
    coach_model = project.manifest.model.for_role("coach")
    per_eval = eval_estimate(agent_model, questions=dev_n).total_usd
    per_coach = coach_estimate(coach_model, failures=dev_n).total_usd
    header = "dry run — no API calls" if dry_run else "estimated spend before you start"
    print(f"▸ optimize ({header}):\n")
    print(f"  baseline dev eval    ~${per_eval:.4f} ({dev_n} question(s))")
    print(f"  each round           ≈ coach ~${per_coach:.4f} + trial dev eval(s) ~${per_eval:.4f}")
    print(f"  final held-out eval  ~${per_eval:.4f} (scored once on the picked version)")
    cap = f"${budget:.2f}" if budget is not None else "(none)"
    print(f"\n  the loop hard-stops before any step would exceed --budget {cap}")
    if not dry_run:
        print("  it EDITS your project files in place (never commits) — review with `git diff`.")


def _warn_if_dirty(project: Project) -> None:
    """Best-effort: warn if the working tree already has uncommitted changes, so the optimizer's
    machine edits don't silently intermingle with the user's own (responsible-ai)."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", str(project.root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if out.returncode == 0 and out.stdout.strip():
        print(
            "  ⚠ your working tree has uncommitted changes — commit or stash first so the "
            "optimizer's edits stay distinguishable from yours.\n"
        )


def _confirm(question: str, *, prompt: object = None) -> bool:
    ask_fn = prompt if prompt is not None else input
    assert callable(ask_fn)
    return str(ask_fn(f"\n{question} [y/N] ")).strip().lower().startswith("y")


def _report_optimize(result: OptimizeResult) -> int:
    picked = result.picked_point
    print(
        f"\n▸ optimize — {result.stopped.value} after {result.rounds} round(s) · "
        f"${result.spent_usd:.4f} spent · {result.models}"
    )
    print(
        f"  edits: {result.edits_tried} tried, {result.edits_kept} kept, "
        f"{result.edits_reverted} reverted"
    )
    print(f"\nfrontier ({len(result.frontier)} version(s), dev accuracy):")
    for pt in result.frontier:
        star = " ← picked" if pt.version == result.picked else ""
        if pt.proposal_title:
            sig = "" if pt.significant else " ⚠ within noise"
            edit = f"  +{pt.net_gain}q  {pt.proposal_title} [{pt.layer}]{sig}"
        else:
            edit = "  (baseline)"
        print(
            f"  v{pt.version}  {pt.dev_accuracy:.0%} "
            f"(95% CI {pt.dev_accuracy_low:.0%}–{pt.dev_accuracy_high:.0%}, n={pt.dev_n}) · "
            f"${pt.dev_cost_usd:.4f} · {pt.mean_latency_ms:.0f}ms{star}\n{edit}"
        )
    reached = "reached" if picked.dev_accuracy >= result.target else "not reached"
    print(
        f"\npicked v{result.picked}: dev {picked.dev_accuracy:.0%} "
        f"(target {result.target:.0%}, {reached})"
    )
    # A dev gain that doesn't clear the paired sign test is likely noise on a small set — say so
    # plainly rather than presenting an optimized-against number as settled (ml-systems).
    if result.picked > 0 and not result.picked_significant:
        print(
            "  ⚠ the dev gain over baseline is NOT statistically distinguishable from noise on "
            "this set — don't trust it; the held-out number below is authoritative."
        )
    if result.test_accuracy is not None:
        print(f"  held-out test {result.test_accuracy:.0%} (n={result.test_n}, scored once)")
        gap = result.dev_test_gap or 0.0
        if gap > 0.1:
            print(
                f"  ⚠ dev↔test gap {gap:+.0%} — the loop may have overfit dev; the held-out "
                "number is the one to trust (spec §7/§11)."
            )
    print("\nreview the edits with `git diff`; revert with `git checkout` (never auto-committed).")
    return 0


def _ask(args: list[str]) -> int:
    """`sqbyl ask "question" [DIR] [--replay PATH]` — answer one question end-to-end."""
    from sqbyl.estimates import ask_estimate
    from sqbyl.llm import build_llm_client
    from sqbyl.project import Project
    from sqbyl.projectfiles import load_knowledge
    from sqbyl_runtime.pipeline import ask
    from sqbyl_runtime.state.layout import SqbylPaths
    from sqbyl_runtime.state.traces import TraceWriter

    budget_parse = _budget_opts(args)
    if budget_parse is None:
        return 2
    budget, auto, dry_run = budget_parse
    replay = _opt(args, "replay")
    positional = [a for a in args if not a.startswith("-") and a != replay]
    if not positional:
        print('usage: sqbyl ask "your question" [DIR] [--replay cassette.json]')
        return 2
    question = positional[0]
    project = Project.load(positional[1] if len(positional) > 1 else ".")
    model = project.manifest.model.for_role("agent")

    estimate = ask_estimate(
        model, self_repair_attempts=project.manifest.defaults.self_repair_attempts
    )
    if dry_run:
        print(f"▸ ask (dry run — no API calls):\n\n{estimate.render()}")
        return 0
    cap = f" · budget ${budget:.2f}" if budget is not None else ""
    print(f"▸ asking on {model} — estimated ~${estimate.total_usd:.4f} (paid){cap}")
    # A single pre-estimated call: gate it up front so `ask` honors the uniform budget
    # contract (auto hard-stops; guided asks) like every other paid command.
    if budget is not None and estimate.total_usd > budget + 1e-9:
        if auto:
            print(
                f"  ✗ estimate ~${estimate.total_usd:.4f} exceeds budget ${budget:.2f} — stopping"
            )
            return 1
        answer = input(
            f"  ⏸ estimate ~${estimate.total_usd:.4f} exceeds the ${budget:.2f} budget. "
            "Proceed anyway? [y/N] "
        )
        if not answer.strip().lower().startswith("y"):
            print("  aborted — nothing spent")
            return 1

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

    from sqbyl.estimates import eval_estimate
    from sqbyl.eval.benchmarks_io import Split
    from sqbyl.eval.heldout import load_for_eval
    from sqbyl.eval.report import (
        diff_runs,
        latest_run,
        load_runs,
        overfitting_signal,
        previous_run,
    )
    from sqbyl.models import Verdict
    from sqbyl.project import Project
    from sqbyl_runtime.state.layout import SqbylPaths

    budget_parse = _budget_opts(args)
    if budget_parse is None:
        return 2
    budget, auto, dry_run = budget_parse
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

    judging = project.manifest.automation.auto_judge
    judge_model = project.manifest.model.for_role("judge") if judging else None
    estimate = eval_estimate(
        model,
        questions=len(questions),
        judge_model=judge_model,
        self_repair_attempts=project.manifest.defaults.self_repair_attempts,
    )
    if dry_run:
        print(f"▸ eval {split.value} (dry run — no API calls):\n\n{estimate.render()}")
        return 0
    judge_note = (
        " (agent + a bounded judge allowance per review-pile row, all metered live)"
        if judging
        else ""
    )
    cap = f" · budget ${budget:.2f}" if budget is not None else ""
    print(
        f"▸ eval {split.value} — {len(questions)} question(s) on {model}, "
        f"estimated ~${estimate.total_usd:.4f} (paid){judge_note}{cap}"
    )
    # A bounded single pass: gate on the whole-run estimate up front. Auto hard-stops;
    # guided asks. (Live mid-run capping arrives with the orchestrated `init`, Phase 7.2.)
    if budget is not None and estimate.total_usd > budget + 1e-9:
        if auto:
            print(
                f"  ✗ estimate ~${estimate.total_usd:.4f} exceeds budget ${budget:.2f} — stopping"
            )
            return 1
        answer = input(
            f"  ⏸ estimate ~${estimate.total_usd:.4f} exceeds the ${budget:.2f} budget. "
            "Proceed anyway? [y/N] "
        )
        if not answer.strip().lower().startswith("y"):
            print("  aborted — nothing spent")
            return 1

    run = project.eval(split.value, replay=replay, record=record, as_of=as_of)

    # Headline accuracy is DETERMINISTIC only — the truth users report upstream. The judge
    # is advisory and never moves this number (see spec §7 / the review pile below).
    lo, hi = run.accuracy_ci()
    print(
        f"\naccuracy (deterministic): {run.n_correct}/{run.total} ({run.accuracy:.1%}"
        f", 95% CI {lo:.0%}–{hi:.0%})"
        f" · needs review: {run.n_manual_review} · errors: {run.n_error}"
    )
    if run.n_manual_review:
        # Advisory triage: how the judge thinks the review pile splits (never scored).
        likely_ok = run.n_suggested(Verdict.correct)
        likely_wrong = run.n_suggested(Verdict.incorrect)
        ambiguous = run.n_suggested(Verdict.manual_review)
        print(
            f"  review pile — judge suggests: {likely_ok} likely-equivalent, "
            f"{likely_wrong} likely-wrong, {ambiguous} ambiguous (all await a human)"
        )
    print(
        f"self-repair: {run.self_repair_rate:.1%} · mean latency: {run.mean_latency_ms:.0f}ms"
        f" · {run.total_tokens} tokens · ${run.total_cost_usd:.4f}"
    )
    print("models: " + ", ".join(f"{role}={m}" for role, m in sorted(run.models.items())))
    if run.models.get("judge") and run.models.get("judge") == run.models.get("agent"):
        print(
            "  ⚠ judge model == agent model — suggestions share the agent's blind spots; "
            "pin a different judge model in sqbyl.yaml for independence."
        )
    marks = {"correct": "✓", "manual_review": "?", "error": "✗"}
    hint = {
        "correct": "likely-equivalent",
        "incorrect": "likely-wrong",
        "manual_review": "ambiguous",
    }
    for r in run.results:
        v = r.verdict.value
        # For a review-pile row, show the advisory judge hint (not a score).
        suggestion = f"  → judge: {hint[r.judge_suggestion.value]}" if r.judge_suggestion else ""
        print(f"  {marks[v]} {r.id}  [{v}]{suggestion}")

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

    # The dev↔test overfitting signal (spec §7): when the honest held-out set is scored, put
    # its accuracy next to the latest dev run and warn if the loop has tuned to dev rather than
    # generalized. This is the backstop that keeps `coach apply` from quietly training on dev.
    if split is Split.test:
        dev_run = latest_run(paths, split=Split.dev.value)
        if dev_run is not None:
            sig = overfitting_signal(dev_run, run)
            print(
                f"\ndev↔test gap: dev {sig.dev_accuracy:.1%} vs test {sig.test_accuracy:.1%} "
                f"(gap {sig.gap:+.1%}, threshold {sig.threshold:.0%})"
            )
            if sig.overfit:
                print(
                    "  ⚠ overfitting: dev is well above held-out test — the improvement loop "
                    "has tuned to the dev set. Trust the test number, and diversify dev."
                )
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
    from sqbyl.estimates import synth_estimate
    from sqbyl.llm import build_llm_client
    from sqbyl.models import DropReason
    from sqbyl.project import Project
    from sqbyl.synth import synthesize
    from sqbyl_runtime.state.layout import SqbylPaths
    from sqbyl_runtime.state.traces import TraceWriter, new_trace_id

    budget_parse = _budget_opts(args)
    if budget_parse is None:
        return 2
    budget, auto, dry_run = budget_parse
    replay, record, model_opt = _opt(args, "replay"), _opt(args, "record"), _opt(args, "model")
    n_opt, as_of_opt = _opt(args, "n"), _opt(args, "as-of")
    consumed = {replay, record, model_opt, n_opt, as_of_opt}
    positional = [a for a in args if not a.startswith("-") and a not in consumed]
    project = Project.load(positional[0] if positional else ".")
    model = model_opt or project.manifest.model.for_role("synth")
    n = int(n_opt) if n_opt else 20
    try:
        as_of = datetime.fromisoformat(as_of_opt) if as_of_opt else None
    except ValueError:
        print(f"invalid --as-of {as_of_opt!r}; expected an ISO datetime like 2026-06-30")
        return 2

    estimate = synth_estimate(model, n=n)
    if dry_run:
        print(f"▸ synth (dry run — no API calls):\n\n{estimate.render()}")
        return 0
    cap = f" · budget ${budget:.2f}" if budget is not None else ""
    print(
        f"▸ synth ~{n} candidate(s) on {model} — estimated ~${estimate.total_usd:.4f} (paid){cap}"
    )
    if budget is not None and estimate.total_usd > budget + 1e-9:
        if auto:
            print(
                f"  ✗ estimate ~${estimate.total_usd:.4f} exceeds budget ${budget:.2f} — stopping"
            )
            return 1
        answer = input(
            f"  ⏸ estimate ~${estimate.total_usd:.4f} exceeds the ${budget:.2f} budget. "
            "Proceed anyway? [y/N] "
        )
        if not answer.strip().lower().startswith("y"):
            print("  aborted — nothing spent (raise --budget or lower --n)")
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


def _coach(args: list[str]) -> int:
    """`sqbyl coach [DIR] [--budget $N] [--replay P] [--record P] [--model M]`.

    The headline loop (spec §8): read the latest **dev** eval run's failures and propose a
    ranked list of applyable file diffs — the minimal, highest-leverage edit at the right
    layer (examples > semantics > prose). One paid call; proposals are saved so
    ``sqbyl coach apply N`` can write them. The Coach never sees ``test.yaml``.
    """
    if args and args[0] == "apply":
        return _coach_apply(args[1:])
    from sqbyl.coach import coach, gather_failures, save_report
    from sqbyl.estimates import coach_estimate
    from sqbyl.eval.report import latest_run
    from sqbyl.llm import build_llm_client
    from sqbyl.project import Project
    from sqbyl_runtime.state.layout import SqbylPaths
    from sqbyl_runtime.state.traces import TraceWriter, new_trace_id

    budget_parse = _budget_opts(args)
    if budget_parse is None:
        return 2
    budget, auto, dry_run = budget_parse
    replay, record, model_opt = _opt(args, "replay"), _opt(args, "record"), _opt(args, "model")
    consumed = {replay, record, model_opt}
    positional = [a for a in args if not a.startswith("-") and a not in consumed]
    project = Project.load(positional[0] if positional else ".")
    model = model_opt or project.manifest.model.for_role("coach")

    paths = SqbylPaths(project.root)
    run = latest_run(paths, split="dev")
    failures = gather_failures(run) if run is not None else []

    if dry_run:
        # One drafting call regardless of failure count; label it with what's on hand.
        rendered = coach_estimate(model, failures=len(failures)).render()
        print(f"▸ coach (dry run — no API calls):\n\n{rendered}")
        return 0
    if run is None:
        print("no dev run yet — run `sqbyl eval dev` first, then coach its failures")
        return 1
    if not failures:
        print(f"dev run {run.run_id[:8]} is clean ({run.n_correct}/{run.total}) — nothing to coach")
        return 0

    estimate = coach_estimate(model, failures=len(failures))
    cap = f" · budget ${budget:.2f}" if budget is not None else ""
    print(
        f"▸ coach {len(failures)} dev failure(s) from run {run.run_id[:8]} on {model} — "
        f"estimated ~${estimate.total_usd:.4f} (paid){cap}"
    )
    if budget is not None and estimate.total_usd > budget + 1e-9:
        if auto:
            print(
                f"  ✗ estimate ~${estimate.total_usd:.4f} exceeds budget ${budget:.2f} — stopping"
            )
            return 1
        answer = input(
            f"  ⏸ estimate ~${estimate.total_usd:.4f} exceeds the ${budget:.2f} budget. "
            "Proceed anyway? [y/N] "
        )
        if not answer.strip().lower().startswith("y"):
            print("  aborted — nothing spent (raise --budget)")
            return 1

    llm = build_llm_client(project.manifest, replay=replay, record=record)
    report = coach(
        project,
        run,
        llm=llm,
        model=model,
        trace_writer=TraceWriter(paths.ensure().traces_dir / "coach.jsonl"),
    )
    spent = _meter(
        project, report.usage, model=model, command="coach", role="coach", run_id=new_trace_id()
    )
    save_report(paths, report)

    # How much of the target set is trustworthy: an unresolved mismatch may be a false
    # failure (the agent's SQL could be equivalent), so surface it rather than implying every
    # coached row is a real bug (spec §7 — a mismatch is not proof of incorrectness).
    unresolved = sum(1 for r in failures if r.needs_review and r.human_verdict is None)
    noise = f" · {unresolved} unresolved mismatch(es) — may be false failures" if unresolved else ""
    print(
        f"\nsqbyl Coach — {report.n_failures} failing{noise} · {report.n_proposals} proposal(s) · "
        f"predicts ~{report.total_predicted_fixes} fix(es) (model estimate, unverified) · "
        f"${spent:.4f}\n"
    )
    for i, p in enumerate(report.proposals, start=1):
        flag = (
            "  ⚠ global prose — last resort"
            if p.is_prose
            else "  ⚠ single-question example — memorization risk"
            if p.memorization_risk
            else ""
        )
        print(f"[{i}] {p.title}   → {p.target_file}{flag}")
        # predicted_fixes / confidence are the model's OWN unvalidated guesses (ml-systems):
        # label them as such, not as a measured, calibrated leverage score.
        print(
            f"    predicts ~{p.predicted_fixes} fix(es) · confidence {p.confidence:.0%} "
            f"(self-reported, unverified) · root cause: {p.root_cause}"
        )
        if p.conflicts:
            print(f"    ⚠ conflict: {p.conflicts}")
        for line in p.render_diff().splitlines():
            print(f"    {line}")
        print()
    print(
        "⚠ These edits raise the DEV score, which is UNVALIDATED until you re-score the "
        "held-out test set.\n"
        "  A rising dev with a flat test is overfitting — after `coach apply`, run "
        "`sqbyl eval test` and watch the dev↔test gap.\n"
        "apply with: sqbyl coach apply N [M ...]"
    )
    return 0


def _coach_apply(args: list[str]) -> int:
    """`sqbyl coach apply N [M ...] [DIR]` — write chosen proposals from the latest report.

    Applies the picked proposals' find/replace edits to the project files ($0, no LLM). The
    result is an ordinary working-tree change: review with ``git diff``, undo with
    ``git checkout``/``git revert``. Then re-run ``eval dev`` to see the targeted questions
    flip, and ``eval test`` to check the held-out number actually moved."""
    from datetime import UTC, datetime

    from sqbyl.coach import ApplyError, apply_proposal, latest_report, save_report
    from sqbyl.project import Project
    from sqbyl_runtime.state.layout import SqbylPaths

    force = "--force" in args
    indices = [int(a) for a in args if a.isdigit()]
    positional = [a for a in args if not a.startswith("-") and not a.isdigit()]
    project = Project.load(positional[0] if positional else ".")
    paths = SqbylPaths(project.root)
    report = latest_report(paths)
    if report is None:
        print("no coach report yet — run `sqbyl coach` first")
        return 1
    if not indices:
        print("usage: sqbyl coach apply N [M ...]  (proposal numbers from `sqbyl coach`)")
        return 2

    applied, changed, failed = 0, set(), 0
    for n in indices:
        if not 1 <= n <= report.n_proposals:
            print(f"  ✗ no proposal [{n}] (report has {report.n_proposals})")
            failed += 1
            continue
        proposal = report.proposals[n - 1]
        # Refuse a re-apply (an empty-`find` append would silently duplicate the edit); the
        # record of what was applied lives on the persisted report.
        if proposal.applied_at is not None and not force:
            print(f"  · [{n}] {proposal.title}: already applied — skipping (--force to re-apply)")
            continue
        try:
            path = apply_proposal(project, proposal, force=force)
        except ApplyError as exc:
            print(f"  ✗ [{n}] {proposal.title}: {exc}")
            failed += 1
            continue
        proposal.applied_at = datetime.now(UTC)  # stamp the audit trail (persisted below)
        rel = path.relative_to(project.root.resolve())
        print(f"  ✓ [{n}] {proposal.title} → {rel}")
        changed.add(str(rel))
        applied += 1

    if applied:
        save_report(paths, report)  # persist the applied markers back onto the report
        print(
            f"\napplied {applied} proposal(s) to {len(changed)} file(s). Review with "
            "`git diff`; undo with `git checkout -- <file>`.\n"
            "Then: `sqbyl eval dev` (see the targeted questions flip) → `sqbyl eval test` "
            "(confirm the held-out number moved, not just dev)."
        )
    return 1 if failed and not applied else 0


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
    from sqbyl.estimates import annotate_estimate
    from sqbyl.llm import build_llm_client
    from sqbyl.project import Project
    from sqbyl.semantics_io import dump_yaml_path, merge_annotation
    from sqbyl.yamlio import load_yaml
    from sqbyl_runtime.cost import SpendMeter
    from sqbyl_runtime.models import TableSemantics
    from sqbyl_runtime.state.layout import SqbylPaths
    from sqbyl_runtime.state.traces import Span, TraceWriter, new_span_id, new_trace_id
    from sqbyl_runtime.state.usage import UsageStore

    budget_parse = _budget_opts(args)
    if budget_parse is None:
        return 2
    budget, auto, dry_run = budget_parse
    replay, record, model_opt = _opt(args, "replay"), _opt(args, "record"), _opt(args, "model")
    consumed = {replay, record, model_opt}
    positional = [a for a in args if not a.startswith("-") and a not in consumed]
    project = Project.load(positional[0] if positional else ".")
    model = model_opt or project.manifest.model.default

    paths = sorted(project.semantics_dir.glob("*.yaml"))
    if not paths:
        print("no semantics/*.yaml found — run `sqbyl introspect` and `sqbyl profile` first")
        return 1

    estimate = annotate_estimate(model, tables=len(paths))
    if dry_run:
        print(f"▸ annotate (dry run — no API calls):\n\n{estimate.render()}")
        return 0
    per_table = annotate_estimate(model, tables=1).total_usd
    cap = f" · budget ${budget:.2f}" if budget is not None else ""
    print(
        f"▸ annotating {len(paths)} table(s) on {model} — "
        f"estimated ~${estimate.total_usd:.4f} (paid){cap}"
    )

    llm = build_llm_client(project.manifest, replay=replay, record=record)
    state = SqbylPaths(project.root).ensure()
    trace_writer = TraceWriter(state.traces_dir / "annotate.jsonl")
    run_span = Span(
        name="annotate",
        trace_id=new_trace_id(),
        span_id=new_span_id(),
        attributes={"gen_ai.operation.name": "chat", "sqbyl.tables": len(paths)},
    )

    done, stopped = 0, False
    with UsageStore(state.usage_db) as store:
        meter = SpendMeter(budget=budget, store=store, command="annotate")
        for path in paths:
            # Live cap: gate each table on the running tally before spending on it.
            if not _authorize(meter, per_table, auto=auto, label=f"annotate {path.name}"):
                left = len(paths) - done
                print(f"  ⏸ {left} table(s) left; re-run `sqbyl annotate` to continue")
                stopped = True
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
            meter.record(response.usage, model=model, role="annotator", run_id=run_span.trace_id)
            done += 1
            print(f"  ✓ {path.name}  (table confidence {annotation.confidence:.2f})")
        spent = meter.spent
    trace_writer.write(run_span.end(status="ok" if not stopped else "error"))
    print(f"done — annotated {done}/{len(paths)}, metered ${spent:.4f}")
    return 0


def _init(args: list[str]) -> int:
    """`sqbyl init [DIR] [--auto --budget $N] [--dry-run] [--model M] [--select STEPS] [--n N]`.

    The guided push (spec §5.5): a free deterministic pass ($0), then a costed plan you
    confirm, then orchestrated paid enrichment ending in the attention queue. ``--auto`` runs
    it headless (``--budget`` required); ``--dry-run`` shows the plan and spends nothing;
    ``--select`` keeps a subset of ``annotate,synth,eval``; guided runs prompt to proceed,
    swap to a cheaper model (``m``), pick steps (``s``), or bail (``n``).
    """
    from datetime import datetime

    from sqbyl import init as initmod
    from sqbyl.llm import build_llm_client
    from sqbyl.orchestrator import Orchestrator
    from sqbyl.project import Project
    from sqbyl_runtime.cost import SpendMeter
    from sqbyl_runtime.state.layout import SqbylPaths
    from sqbyl_runtime.state.usage import UsageStore

    budget_parse = _budget_opts(args)
    if budget_parse is None:
        return 2
    budget, auto, dry_run = budget_parse
    replay, record = _opt(args, "replay"), _opt(args, "record")
    model_opt, select_opt = _opt(args, "model"), _opt(args, "select")
    n_opt, as_of_opt = _opt(args, "n"), _opt(args, "as-of")
    consumed = {replay, record, model_opt, select_opt, n_opt, as_of_opt}
    positional = [a for a in args if not a.startswith("-") and a not in consumed]
    project = Project.load(positional[0] if positional else ".")

    try:
        as_of = datetime.fromisoformat(as_of_opt) if as_of_opt else None
    except ValueError:
        print(f"invalid --as-of {as_of_opt!r}; expected an ISO datetime like 2026-06-30")
        return 2
    synth_n = int(n_opt) if n_opt else 20
    model = model_opt or project.manifest.model.default
    steps = tuple(s.strip() for s in select_opt.split(",")) if select_opt else initmod.STEPS
    if any(s not in initmod.STEPS for s in steps):
        print(f"--select must be a comma list of {','.join(initmod.STEPS)}")
        return 2

    # ── Phase 1: the free pass ($0) ──
    print("▸ sqbyl init — free pass (read-only SQL, $0)")
    free = initmod.run_free_pass(project)
    print(
        f"  ✓ {free.n_tables} table(s), {free.n_columns} column(s) profiled · "
        f"{free.joins} join candidate(s) ({free.ambiguous_joins} ambiguous)"
    )

    plan = initmod.build_plan(project, free, model=model, steps=steps, synth_n=synth_n, as_of=as_of)
    if not plan.has_paid_work:
        print("  ✓ nothing to enrich — the project is already up to date ($0)")
        return 0

    print("\n  Ready to enrich with Claude. Here's the plan and the estimate:\n")
    print(plan.estimate.render(indent="    "))

    if dry_run:
        print("\n(dry run — no API calls made)")
        return 0

    # ── Confirm (guided prompts; --auto proceeds headless within its required budget) ──
    if not auto:
        confirmed = _confirm_init_plan(project, free, plan, synth_n=synth_n, steps=steps)
        if confirmed is None:
            print("  aborted — nothing spent")
            return 0
        plan = confirmed
        if not plan.has_paid_work:
            print("  no steps selected — nothing spent")
            return 0

    # ── Phase 2: orchestrated enrichment, live-metered ──
    print("\n▸ enriching (metering live)…")
    paths = SqbylPaths(project.root).ensure()
    llm = build_llm_client(project.manifest, replay=replay, record=record)
    with UsageStore(paths.usage_db) as store:
        meter = SpendMeter(budget=budget, store=store, command="init")
        result = initmod.enrich(
            project,
            plan,
            llm=llm,
            meter=meter,
            orchestrator=Orchestrator(concurrency=4),
            authorize=lambda m, cost, label: _authorize(m, cost, auto=auto, label=label),
            schema_fingerprint=free.schema_fingerprint,
            replay=replay,
            record=record,
            as_of=as_of,
        )
    return _report_init_arrival(result)


def _confirm_init_plan(
    project: Project,
    free: FreePass,
    plan: InitPlan,
    *,
    synth_n: int,
    steps: tuple[str, ...],
) -> InitPlan | None:
    """The guided ``[Y]es / [s]elect steps / [m]odel / [n]o`` menu (spec §5.5). None = bail."""
    from sqbyl import init as initmod

    current = plan
    while True:
        choice = (
            input("\n  Proceed? [Y]es · [s]elect steps · [m]odel (cheaper) · [n]o: ")
            .strip()
            .lower()
        )
        if choice in ("", "y", "yes"):
            return current
        if choice in ("n", "no"):
            return None
        if choice.startswith("m"):
            new_model = input("    model id (e.g. claude-haiku-4-5-20251001): ").strip()
            if new_model:
                current = initmod.build_plan(
                    project, free, model=new_model, steps=steps, synth_n=synth_n
                )
                print(f"\n  Re-estimated on {new_model}:\n")
                print(current.estimate.render(indent="    "))
        elif choice.startswith("s"):
            picked = input(f"    steps to keep ({','.join(initmod.STEPS)}): ").strip()
            kept = tuple(s.strip() for s in picked.split(",") if s.strip() in initmod.STEPS)
            current = initmod.build_plan(
                project, free, model=current.model, steps=kept or (), synth_n=synth_n
            )
            print(f"\n  Plan for [{', '.join(kept) or 'none'}]:\n")
            print(current.estimate.render(indent="    "))
        else:
            print("    (unrecognized — y to proceed, n to bail)")


def _report_init_arrival(result: EnrichmentResult) -> int:
    """Print the arrival summary: what ran, the readiness meter, and the leverage queue."""
    print(f"\n▸ enrichment complete — metered ${result.spent_usd:.4f}")
    if result.annotated:
        print(f"  ✓ annotated {result.annotated} table(s)")
    for label, err in result.annotate_failures:
        print(f"  ⚠ {label}: {err.splitlines()[0]} (surfaced as a card)")
    if result.survivors:
        # Be explicit that this is an automatic, reversible action on machine-authored gold —
        # the human confirms (or edits) it in `sqbyl review`; nothing here is final.
        print(
            f"  ✓ auto-accepted {result.survivors} unreviewed question(s) into the dev set "
            "→ confirm the gold in `sqbyl review`"
        )
    if result.run is not None:
        r = result.run
        # The dev set is self-generated and its gold isn't human-confirmed yet, so this is a
        # provisional agreement rate, not a validated accuracy (ml-systems / responsible-ai).
        provisional = " (provisional — gold self-generated, unreviewed)" if result.survivors else ""
        line = f"  ✓ baseline eval: {r.n_correct}/{r.total} ({r.accuracy:.0%}){provisional}"
        if result.queue is not None and result.queue.readiness.low_confidence:
            lo, hi = result.queue.readiness.accuracy_low, result.queue.readiness.accuracy_high
            line += (
                f" · small sample (n={r.total}, 95% CI {lo:.0%}–{hi:.0%} — treat as directional)"
            )
        print(line)
        print(f"    {r.n_manual_review} row(s) to review")
    if result.stopped:
        print("  ⏸ stopped at the budget — re-run `sqbyl init` to continue where it left off")

    queue = result.queue
    if queue is not None:
        print(f"\n  readiness: {queue.readiness.headline()}")
        if queue.auto_applied:
            # Auto-apply gates on an as-yet-uncalibrated confidence threshold — say so, and
            # point at the one-click undo.
            print(
                f"  auto-applied {len(queue.auto_applied)} high-confidence decision(s) "
                "(uncalibrated threshold; undo any in `sqbyl review`)"
            )
        print(f"  {len(queue.queue)} decision(s) need you → run `sqbyl review`")
    return 0


def _serve(args: list[str]) -> int:
    """`sqbyl serve [DIR] [--host H] [--port N] [--budget $X]` — local chat over the project."""
    from sqbyl.project import Project
    from sqbyl.serve import ChatServer, project_endpoint
    from sqbyl_runtime.state.layout import SqbylPaths

    host = _opt(args, "host") or "127.0.0.1"
    port = int(_opt(args, "port") or "8765")
    budget_parse = _budget_opts(args)
    if budget_parse is None:
        return 2
    budget, _auto, _dry = budget_parse
    positional = [a for a in args if not a.startswith("-")]
    # Drop values consumed by --host/--port/--budget so a bare DIR is what remains.
    consumed = {host, str(port)} | ({str(budget)} if budget is not None else set())
    positional = [p for p in positional if p not in consumed]
    project = Project.load(positional[0] if positional else ".")
    endpoint = project_endpoint(project)
    chat = ChatServer(endpoint, paths=SqbylPaths(project.root), budget=budget)
    return _serve_forever(chat, host=host, port=port, budget=budget)


def _run(args: list[str]) -> int:
    """`sqbyl run <release.json> --db URL --model M [--mcp] [--host] [--port] [--budget] [--root]`.

    Default: an HTTP chat server (like `serve`, but over a release). ``--mcp`` instead serves
    the release as an MCP tool over stdio (for an MCP client to launch as a subprocess).
    """
    from pathlib import Path

    from sqbyl.serve import ChatServer, release_endpoint
    from sqbyl_runtime.state.layout import SqbylPaths

    positional = [a for a in args if not a.startswith("-")]
    mcp = "--mcp" in args
    db = _opt(args, "db")
    model = _opt(args, "model")
    root = _opt(args, "root") or "."
    host = _opt(args, "host") or "127.0.0.1"
    port = int(_opt(args, "port") or "8765")
    row_cap = int(_opt(args, "row-cap") or "100")
    budget_parse = _budget_opts(args)
    if budget_parse is None:
        return 2
    budget, _auto, _dry = budget_parse
    known = {db, model, root, host, str(port), str(row_cap)}
    known |= {str(budget)} if budget is not None else set()
    rel = [p for p in positional if p not in known]
    if not rel or db is None or model is None:
        print("usage: sqbyl run <release.json> --db URL --model MODEL [--mcp] [--budget $X]")
        return 2
    if mcp:
        # An MCP server is an autonomous, human-out-of-the-loop paid consumer (a downstream
        # agent decides call volume), so it must have a hard cap — the same rule optimize and
        # --auto enforce (responsible-ai). The interactive HTTP path has a human, so it doesn't.
        if budget is None:
            print(
                "--mcp requires --budget $N: an MCP client makes unbounded paid calls with no "
                "human in the loop, so it must have a hard cap to stop at"
            )
            return 2
        return _run_mcp(rel[0], db=db, model=model, root=root, budget=budget, row_cap=row_cap)
    endpoint = release_endpoint(rel[0], db=db, model=model, project_root=root)
    chat = ChatServer(endpoint, paths=SqbylPaths(Path(root)), budget=budget)
    return _serve_forever(chat, host=host, port=port, budget=budget)


def _run_mcp(
    release: str, *, db: str, model: str, root: str, budget: float | None, row_cap: int = 100
) -> int:
    """Serve a release as an MCP tool over stdio, metering each tool call to usage.db.

    ``row_cap`` bounds how many result rows are returned to the MCP client (0 = SQL + row
    count only, no rows) — since an MCP consumer is a downstream LLM/app, not the data
    owner's own browser, this lets a launch dial down how much real data leaves.
    """
    from pathlib import Path

    from sqbyl.serve import price_usage_estimate
    from sqbyl_runtime.cost import SpendMeter
    from sqbyl_runtime.export import answer_dict, serve_mcp_stdio
    from sqbyl_runtime.runtime import load
    from sqbyl_runtime.state.layout import SqbylPaths
    from sqbyl_runtime.state.traces import TraceWriter
    from sqbyl_runtime.state.usage import UsageRecord, UsageStore

    paths = SqbylPaths(Path(root)).ensure()
    agent = load(
        release, db=db, model=model, trace_writer=TraceWriter(paths.traces_dir / "mcp.jsonl")
    )
    meter = SpendMeter(budget=budget, store=None, command="run")
    est = price_usage_estimate(model)

    def _metered(question: str) -> dict[str, object]:
        # Budget hard-stop before spending — an unbounded MCP session can't silently overspend.
        if meter.would_exceed(est):
            return {
                "ok": False,
                "sql": "",
                "columns": [],
                "rows": [],
                "row_count": 0,
                "truncated": False,
                "used_assets": [],
                "error": f"session budget ${budget:.2f} reached (${meter.spent:.4f} spent)",
                "trace_id": "",
            }
        result = agent.ask(question)
        cost = meter.record(result.usage, model=model, role="agent", run_id=result.trace_id)
        with UsageStore(paths.usage_db) as store:
            store.record(
                UsageRecord.from_usage(
                    result.usage,
                    model=model,
                    command="run",
                    role="agent",
                    cost_usd=cost,
                    run_id=result.trace_id,
                )
            )
        return answer_dict(result, row_cap=row_cap)

    # stdio carries the JSON-RPC protocol; status/consent goes to stderr so it never
    # corrupts the protocol stream on stdout.
    rows_note = f"up to {row_cap} result rows" if row_cap else "no result rows (SQL + count only)"
    print(
        f"▸ sqbyl MCP server for {Path(release).name} on stdio — each tool call is a paid LLM "
        f"call (~${est:.4f} est on {model}) · session cap ${budget:.2f} · returns {rows_note} "
        "to the MCP client",
        file=sys.stderr,
    )
    try:
        serve_mcp_stdio(agent, call=_metered)
    finally:
        agent.close()
    return 0


def _serve_forever(chat: object, *, host: str, port: int, budget: float | None) -> int:
    """Start the HTTP server and block until interrupted; shared by serve and run."""
    from sqbyl.serve import ChatServer, is_local_host, make_server, price_usage_estimate

    assert isinstance(chat, ChatServer)
    per_call = price_usage_estimate(chat.endpoint.model)
    server = make_server(chat, host=host, port=port)
    print(f"▸ sqbyl serving {chat.endpoint.label} on http://{host}:{port}")
    print(
        f"  each question is a paid call (~${per_call:.4f} est on {chat.endpoint.model}), "
        f"metered to .sqbyl/usage.db"
        + (f" · session cap ${budget:.2f}" if budget is not None else "")
    )
    if not is_local_host(host):
        # Not hardened: no auth, no TLS, no rate limiting. Binding off-localhost exposes the
        # user's database to anyone who can reach the port (spec §9.2).
        print(
            f"  ⚠ bound to {host} (not localhost): this server has NO auth and is not hardened — "
            "do not expose it on an untrusted network."
        )
    print("  press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n▸ stopping…")
    finally:
        server.shutdown()
        server.server_close()
        chat.close()
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
    if args and args[0] == "coach":
        return _coach(args[1:])
    if args and args[0] == "cost":
        return _cost(args[1:])
    if args and args[0] == "init":
        return _init(args[1:])
    if args and args[0] == "report":
        return _report(args[1:])
    if args and args[0] == "release":
        return _release(args[1:])
    if args and args[0] == "optimize":
        return _optimize(args[1:])
    if args and args[0] == "serve":
        return _serve(args[1:])
    if args and args[0] == "run":
        return _run(args[1:])
    print(
        "sqbyl: commands — init, introspect, profile, annotate, ask, eval, synth, review, "
        "coach, cost, report, release, optimize, serve, run, schema export, version"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
