"""The autonomous Optimizer — ``sqbyl optimize`` (spec §6.C, plan 8.3).

Runs ``coach → apply → eval`` in a loop **against the dev set**, keeping each edit that
raises the dev score and reverting those that don't, until a ``--target`` accuracy or a
``--budget`` is reached. Returns a **frontier** of versions (each a real working-tree diff)
with their dev accuracy/cost/latency, and scores the held-out test **once** on the picked
version — a large dev↔test gap is surfaced as an overfitting warning (spec §7/§11).

**Dev-only, by construction (invariant 3).** The loop only ever evaluates the ``dev`` split
and only ever hands the Coach a dev :class:`~sqbyl.models.ScoredRun`; the held-out set is
touched exactly once, at the end, to score the version you keep — never to steer the search.
This module must never import :mod:`sqbyl.eval.heldout` (enforced by the import-linter
``forbidden`` contract, which lists ``sqbyl.optimize``); the single final test eval goes
through the sanctioned :meth:`Project.eval` door like every other reader.

**Cost, throughout (invariant 5).** Every Coach call and every trial eval is estimated
before it runs and the loop hard-stops before a step would exceed ``--budget``; Coach spend
is metered to ``.sqbyl/usage.db`` and eval spend is metered by :meth:`Project.eval`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqbyl.coach import ApplyError, apply_proposal, coach, gather_failures
from sqbyl.estimates import coach_estimate, eval_estimate
from sqbyl.models.optimize import FrontierPoint, OptimizeResult, StopReason
from sqbyl.models.runs import ScoredRun
from sqbyl.project import Project
from sqbyl.stats import paired_improvement_significant
from sqbyl_runtime.cost import SpendMeter
from sqbyl_runtime.llm.base import LLMClient
from sqbyl_runtime.state.layout import SqbylPaths
from sqbyl_runtime.state.traces import TraceWriter
from sqbyl_runtime.state.usage import UsageStore

# A safety cap so the loop always terminates even under a huge budget with a chatty Coach.
_DEFAULT_MAX_ROUNDS = 10


def optimize(
    project: Project,
    *,
    llm: LLMClient,
    target: float,
    budget: float | None,
    min_gain: int = 1,
    max_rounds: int = _DEFAULT_MAX_ROUNDS,
    as_of: datetime | None = None,
    score_test: bool = True,
) -> OptimizeResult:
    """Hill-climb the dev score by applying Coach edits, keeping only those that help.

    ``target`` is the dev accuracy to stop at; ``budget`` (dollars) hard-stops the loop
    before any step would exceed it (``None`` = uncapped, for direct callers — the CLI
    requires it). ``min_gain`` is the net questions an edit must fix (fixed − broke) to be
    kept — raise it to resist accepting small-sample noise on tiny dev sets. ``score_test``
    runs the single held-out eval on the picked version at the end.
    """
    # Freeze the clock ONCE so every eval in the loop (baseline, trials, final held-out) scores
    # now()-relative gold against one instant — otherwise a calendar rollover mid-loop could
    # masquerade as an edit effect (ml-systems: reproducibility).
    frozen_as_of = as_of or datetime.now(UTC)
    paths = SqbylPaths(project.root).ensure()
    with UsageStore(paths.usage_db) as store:
        return _run(
            project,
            llm=llm,
            target=target,
            budget=budget,
            min_gain=min_gain,
            max_rounds=max_rounds,
            as_of=frozen_as_of,
            score_test=score_test,
            paths=paths,
            store=store,
        )


def _run(
    project: Project,
    *,
    llm: LLMClient,
    target: float,
    budget: float | None,
    min_gain: int,
    max_rounds: int,
    as_of: datetime,
    score_test: bool,
    paths: SqbylPaths,
    store: UsageStore,
) -> OptimizeResult:
    agent_model = project.manifest.model.for_role("agent")
    coach_model = project.manifest.model.for_role("coach")
    self_repair = project.manifest.defaults.self_repair_attempts
    traces = TraceWriter(paths.traces_dir / "optimize.jsonl")
    # Coach spend lands in usage.db via this meter; eval spend is metered by Project.eval, so
    # the budget total is tracked across both in ``spent``.
    coach_meter = SpendMeter(budget=budget, store=store, command="optimize")
    spent = 0.0
    tried = kept = reverted = 0

    # --- baseline: score dev once; it's frontier version 0 --------------------------------
    baseline = _eval_dev(project, llm, as_of=as_of, persist=True)
    best = baseline
    spent += best.total_cost_usd
    frontier = [FrontierPoint.from_run(best, version=0)]
    dev_n = best.total

    stopped = StopReason.converged
    rounds = 0
    while True:
        if best.accuracy >= target:
            stopped = StopReason.target_met
            break
        if rounds >= max_rounds:
            stopped = StopReason.max_rounds
            break
        failures = gather_failures(best)
        coach_cost = coach_estimate(coach_model, failures=len(failures)).total_usd
        if _would_exceed(spent, coach_cost, budget):
            stopped = StopReason.budget_exhausted
            break

        report = coach(project, best, llm=llm, model=coach_model, trace_writer=traces)
        spent += coach_meter.record(
            report.usage, model=coach_model, role="coach", run_id=report.run_id
        )
        if not report.proposals:
            stopped = StopReason.no_proposals
            break

        eval_cost = eval_estimate(agent_model, questions=dev_n, self_repair_attempts=self_repair)
        accepted = False
        budget_hit = False
        for proposal in report.proposals:
            if _would_exceed(spent, eval_cost.total_usd, budget):
                budget_hit = True
                break
            snapshot = _snapshot(project, proposal.target_file)
            if snapshot is None:  # unsafe/unresolvable target — skip, don't spend on it
                continue
            try:
                apply_proposal(project, proposal)
            except ApplyError:
                continue
            tried += 1
            # Once the edit is on disk, ANY exception before the keep/revert decision (e.g. a
            # trial eval that can't reload the just-edited file) must still roll the file back —
            # otherwise a crash leaves a mutated project with no revert path, which is not
            # guaranteed to be a git repo (finding #11). So the snapshot restore lives in a
            # finally, gated on whether the edit was kept, and any unexpected error propagates
            # only AFTER the file is restored.
            kept_this = False
            try:
                trial = _eval_dev(project, llm, as_of=as_of, persist=False)
                spent += trial.total_cost_usd
                fixed, broke = _paired_delta(best, trial)
                if fixed - broke >= min_gain:  # keep-if-it-helped (net questions past the floor)
                    _save(paths, trial)
                    best = trial
                    kept += 1
                    frontier.append(
                        FrontierPoint.from_run(
                            trial,
                            version=len(frontier),
                            net_gain=fixed - broke,
                            significant=paired_improvement_significant(fixed, broke),
                            proposal_title=proposal.title,
                            proposal_id=proposal.id,
                            target_file=proposal.target_file,
                            layer=proposal.layer.value,
                        )
                    )
                    accepted = kept_this = True
            finally:
                if not kept_this:
                    _restore(snapshot)  # revert-if-not (or on any error): files as they were
            if accepted:
                break
            reverted += 1

        rounds += 1
        if budget_hit:
            stopped = StopReason.budget_exhausted
            break
        if not accepted:
            stopped = StopReason.converged
            break

    return _finish(
        project,
        llm,
        frontier=frontier,
        baseline=baseline,
        best=best,
        stopped=stopped,
        rounds=rounds,
        spent=spent,
        target=target,
        as_of=as_of,
        score_test=score_test,
        models={"agent": agent_model, "coach": coach_model},
        edits=(tried, kept, reverted),
    )


def _finish(
    project: Project,
    llm: LLMClient,
    *,
    frontier: list[FrontierPoint],
    baseline: ScoredRun,
    best: ScoredRun,
    stopped: StopReason,
    rounds: int,
    spent: float,
    target: float,
    as_of: datetime,
    score_test: bool,
    models: dict[str, str],
    edits: tuple[int, int, int],
) -> OptimizeResult:
    """Pick the version that IS the working tree (the last, highest-accuracy one — the loop
    only ever kept net improvements) and score the held-out test on it **once**."""
    # The working tree equals the last kept version, so pick it directly rather than an argmax
    # that could disagree under a future non-monotonic policy (code-reviewer).
    picked = len(frontier) - 1
    tried, kept, reverted = edits
    # Is the cumulative dev gain over baseline real, or within noise? Paired sign test on the
    # same dev questions rerun before/after (ml-systems). No edits kept ⇒ trivially not.
    fixed, broke = _paired_delta(baseline, best)
    result = OptimizeResult(
        frontier=frontier,
        picked=picked,
        stopped=stopped,
        rounds=rounds,
        spent_usd=spent,
        target=target,
        models=models,
        picked_significant=picked > 0 and paired_improvement_significant(fixed, broke),
        edits_tried=tried,
        edits_kept=kept,
        edits_reverted=reverted,
    )
    if score_test:
        # The held-out score is the LAST step, after the whole paid loop. A missing test.yaml
        # (FileNotFoundError) or an empty one (0 questions) must NOT crash here and lose the
        # frontier the run just paid for — skip the scoring and record why (finding #13).
        try:
            test = project.eval("test", llm=llm, as_of=as_of, judge=False, persist=True)
        except FileNotFoundError:
            test = None
        if test is not None and test.total > 0:
            result.test_accuracy = test.accuracy
            result.test_n = test.total
            result.dev_test_gap = frontier[picked].dev_accuracy - test.accuracy
            result.spent_usd += test.total_cost_usd
        else:
            result.test_skipped_reason = (
                "no held-out set — add a hand-authored benchmarks/test.yaml to score the "
                "picked version and measure the dev↔test gap (never synthesized, invariant 3)"
            )
    return result


def _paired_delta(before: ScoredRun, after: ScoredRun) -> tuple[int, int]:
    """(fixed, broke) over the same dev questions: how many the edit flipped wrong→right and
    right→wrong. The paired evidence a sign test needs, from the deterministic correct-sets."""
    b, a = before.correct_ids(), after.correct_ids()
    return len(a - b), len(b - a)


# --- dev/test evals go through the sanctioned Project.eval door (never eval.heldout) --------


def _eval_dev(
    project: Project, llm: LLMClient, *, as_of: datetime | None, persist: bool
) -> ScoredRun:
    # judge=False: the loop optimizes the deterministic headline; the advisory judge would only
    # add cost without moving the number it climbs.
    return project.eval("dev", llm=llm, as_of=as_of, judge=False, persist=persist)


# --- keep/revert: snapshot the one file a proposal touches, restore it if the edit didn't help


def _snapshot(project: Project, target_file: str) -> tuple[Path, str | None] | None:
    """Capture a proposal's target file so a rejected edit can be rolled back byte-for-byte.
    Returns ``(path, prior_text_or_None)``, or ``None`` if the target isn't a writable context
    file (so the caller skips it without spending an eval)."""
    from sqbyl.coach import _resolve_target

    try:
        path = _resolve_target(project, target_file)
    except ApplyError:
        return None
    return path, (path.read_text() if path.exists() else None)


def _restore(snapshot: tuple[Path, str | None]) -> None:
    path, prior = snapshot
    if prior is None:
        path.unlink(missing_ok=True)  # the edit created the file → remove it
    else:
        path.write_text(prior)


# --- helpers -------------------------------------------------------------------------------


def _would_exceed(spent: float, next_cost: float, budget: float | None) -> bool:
    return budget is not None and spent + next_cost > budget


def _save(paths: SqbylPaths, run: ScoredRun) -> None:
    from sqbyl.eval.report import save_run

    save_run(paths, run)
