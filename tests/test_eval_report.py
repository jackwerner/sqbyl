"""Phase 3.3/3.4 — run persistence, the flipped-questions diff, and the overfit signal."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqbyl.eval.report import (
    diff_runs,
    load_run,
    overfitting_signal,
    previous_run,
    save_run,
)
from sqbyl.models.runs import QuestionResult, ScoredRun, Verdict
from sqbyl_runtime.state.layout import SqbylPaths


def _q(qid: str, verdict: Verdict) -> QuestionResult:
    return QuestionResult(id=qid, question=qid, verdict=verdict, generated_sql="SELECT 1")


def _run(run_id: str, results: list[QuestionResult], *, when: datetime) -> ScoredRun:
    return ScoredRun(run_id=run_id, split="dev", created_at=when, results=results)


def test_scored_run_aggregates() -> None:
    run = _run(
        "a" * 32,
        [_q("q1", Verdict.correct), _q("q2", Verdict.manual_review), _q("q3", Verdict.error)],
        when=datetime.now(UTC),
    )
    assert run.total == 3
    assert run.n_correct == 1 and run.n_manual_review == 1 and run.n_error == 1
    assert run.accuracy == 1 / 3
    assert run.manual_review_rate == 1 / 3


def test_accuracy_ci_is_a_valid_interval_around_accuracy() -> None:
    run = _run(
        "a" * 32,
        [_q("q1", Verdict.correct), _q("q2", Verdict.correct), _q("q3", Verdict.manual_review)],
        when=datetime.now(UTC),
    )
    lo, hi = run.accuracy_ci()
    assert 0.0 <= lo <= run.accuracy <= hi <= 1.0
    # Small n → a wide interval (not a false-precision point estimate).
    assert hi - lo > 0.3
    # Empty run is well-defined.
    assert _run("b" * 32, [], when=datetime.now(UTC)).accuracy_ci() == (0.0, 0.0)


def test_save_and_reload_roundtrips(tmp_path: Path) -> None:
    paths = SqbylPaths(tmp_path).ensure()
    run = _run("a" * 32, [_q("q1", Verdict.correct)], when=datetime.now(UTC))
    path = save_run(paths, run)
    reloaded = load_run(path)
    assert reloaded.run_id == run.run_id
    assert reloaded.accuracy == run.accuracy
    assert reloaded.results[0].verdict is Verdict.correct


def test_diff_reports_fixed_regressed_and_added() -> None:
    base = _run(
        "a" * 32,
        [_q("q1", Verdict.correct), _q("q2", Verdict.manual_review)],
        when=datetime.now(UTC),
    )
    new = _run(
        "b" * 32,
        [_q("q1", Verdict.manual_review), _q("q2", Verdict.correct), _q("q3", Verdict.correct)],
        when=datetime.now(UTC),
    )
    d = diff_runs(base, new)
    assert d.fixed == ["q2"]  # manual_review → correct
    assert d.regressed == ["q1"]  # correct → manual_review
    assert d.added == ["q3"]
    assert d.removed == []
    assert d.flipped == ["q1", "q2"]


def test_previous_run_finds_the_earlier_same_split_run(tmp_path: Path) -> None:
    paths = SqbylPaths(tmp_path).ensure()
    t0 = datetime.now(UTC)
    older = _run("a" * 32, [_q("q1", Verdict.correct)], when=t0)
    newer = _run("b" * 32, [_q("q1", Verdict.manual_review)], when=t0 + timedelta(seconds=5))
    save_run(paths, older)
    save_run(paths, newer)
    assert previous_run(paths, newer) is not None
    assert previous_run(paths, newer).run_id == older.run_id  # type: ignore[union-attr]
    assert previous_run(paths, older) is None  # nothing before the oldest


def test_overfitting_signal() -> None:
    dev = _run(
        "a" * 32, [_q("q1", Verdict.correct), _q("q2", Verdict.correct)], when=datetime.now(UTC)
    )
    test = _run(
        "b" * 32,
        [_q("q1", Verdict.correct), _q("q2", Verdict.manual_review)],
        when=datetime.now(UTC),
    )
    signal = overfitting_signal(dev, test, threshold=0.1)
    assert signal.dev_accuracy == 1.0
    assert signal.test_accuracy == 0.5
    assert signal.gap == 0.5
    assert signal.overfit is True
