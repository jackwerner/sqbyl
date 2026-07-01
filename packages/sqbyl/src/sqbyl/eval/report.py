"""Run persistence, run diffs, and the overfitting signal (spec §7, plan 3.3/3.4).

A :class:`ScoredRun` is persisted to ``.sqbyl/runs/`` as JSON so runs reload for the
report layer (§7.5) and for regression detection. :func:`diff_runs` reports exactly which
questions a change **fixed** or **broke** between two runs; :func:`overfitting_signal`
surfaces the dev↔test accuracy gap as a first-class number.
"""

from __future__ import annotations

from pathlib import Path

from sqbyl.models.runs import OverfittingSignal, RunDiff, ScoredRun
from sqbyl_runtime.state.layout import SqbylPaths


def save_run(paths: SqbylPaths, run: ScoredRun) -> Path:
    """Persist a run to ``.sqbyl/runs/``. Filename sorts chronologically and by split."""
    paths.ensure()
    filename = f"{run.created_at:%Y%m%dT%H%M%S}-{run.split}-{run.run_id[:8]}.json"
    path = paths.runs_dir / filename
    path.write_text(run.model_dump_json(indent=2) + "\n")
    return path


def load_run(path: str | Path) -> ScoredRun:
    return ScoredRun.model_validate_json(Path(path).read_text())


def load_runs(paths: SqbylPaths, *, split: str | None = None) -> list[ScoredRun]:
    """Every persisted run, oldest first, optionally filtered to one split."""
    runs = [load_run(p) for p in paths.runs_dir.glob("*.json")]
    if split is not None:
        runs = [r for r in runs if r.split == split]
    return sorted(runs, key=lambda r: (r.created_at, r.run_id))


def latest_run(paths: SqbylPaths, *, split: str | None = None) -> ScoredRun | None:
    """The most recent persisted run (optionally for one split), or ``None`` if there are
    none — the run the review console opens onto (spec §6.5/§7)."""
    runs = load_runs(paths, split=split)
    return runs[-1] if runs else None


def previous_run(paths: SqbylPaths, run: ScoredRun) -> ScoredRun | None:
    """The most recent earlier run of the same split — the baseline for a diff."""
    earlier = [
        r
        for r in load_runs(paths, split=run.split)
        if r.run_id != run.run_id and (r.created_at, r.run_id) < (run.created_at, run.run_id)
    ]
    return earlier[-1] if earlier else None


def diff_runs(base: ScoredRun, new: ScoredRun) -> RunDiff:
    """Which questions flipped between ``base`` and ``new`` (regression detection, §7)."""
    base_by = {r.id: r.correct for r in base.results}
    new_by = {r.id: r.correct for r in new.results}
    fixed, regressed, still_passing, still_failing = [], [], [], []
    for qid in base_by.keys() & new_by.keys():
        was, now = base_by[qid], new_by[qid]
        if now and not was:
            fixed.append(qid)
        elif was and not now:
            regressed.append(qid)
        elif was and now:
            still_passing.append(qid)
        else:
            still_failing.append(qid)
    return RunDiff(
        from_run_id=base.run_id,
        to_run_id=new.run_id,
        fixed=sorted(fixed),
        regressed=sorted(regressed),
        still_passing=sorted(still_passing),
        still_failing=sorted(still_failing),
        added=sorted(new_by.keys() - base_by.keys()),
        removed=sorted(base_by.keys() - new_by.keys()),
    )


def overfitting_signal(
    dev: ScoredRun, test: ScoredRun, *, threshold: float = 0.1
) -> OverfittingSignal:
    """The dev↔test accuracy gap. A large positive gap means the loop overfit the dev
    set rather than generalizing (spec §7, plan 3.4)."""
    return OverfittingSignal(
        dev_accuracy=dev.accuracy, test_accuracy=test.accuracy, threshold=threshold
    )
