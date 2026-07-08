"""Finding #4 — the Optimizer's trials aggregation (`_judge_trials`, spec §7).

Hosted-model inference is nondeterministic even at temperature 0, so a single trial eval's
paired delta can be an edit effect *or* sampling noise. ``--trials N`` re-runs each candidate
and keeps it only when a strict majority of trials clear ``min_gain``; ``require_significant``
adds the sign test. These unit-test that keep/revert logic on synthetic runs (no LLM).
"""

from __future__ import annotations

from sqbyl.models.runs import QuestionResult, ScoredRun, Verdict
from sqbyl.optimize import _judge_trials


def _run(correct: set[str], all_ids: set[str]) -> ScoredRun:
    results = [
        QuestionResult(
            id=i,
            question="q",
            verdict=Verdict.correct if i in correct else Verdict.manual_review,
            generated_sql="x",
        )
        for i in sorted(all_ids)
    ]
    return ScoredRun(run_id=f"r-{sorted(correct)}", split="dev", results=results)


_ALL = {"a", "b", "c", "d", "e", "f"}


def test_single_trial_matches_the_old_gate() -> None:
    best = _run({"a"}, _ALL)
    helped = _run({"a", "b"}, _ALL)  # net +1
    d = _judge_trials(best, [helped], min_gain=1, require_significant=False)
    assert d.keep and d.net_gain == 1

    no_help = _run({"a"}, _ALL)  # net 0
    assert not _judge_trials(best, [no_help], min_gain=1, require_significant=False).keep


def test_majority_of_trials_must_clear_min_gain() -> None:
    best = _run({"a"}, _ALL)
    helped = _run({"a", "b"}, _ALL)  # +1
    flat = _run({"a"}, _ALL)  # 0
    # 2 of 3 clear the bar → keep; representative is the median net gain (1).
    keep = _judge_trials(best, [helped, helped, flat], min_gain=1, require_significant=False)
    assert keep.keep and keep.net_gain == 1
    # only 1 of 3 clears → revert (a lone noisy trial can't ratchet it in).
    revert = _judge_trials(best, [helped, flat, flat], min_gain=1, require_significant=False)
    assert not revert.keep


def test_representative_is_the_median_not_the_luckiest() -> None:
    best = _run({"a"}, _ALL)
    trials = [_run({"a"}, _ALL), _run({"a", "b"}, _ALL), _run({"a", "b", "c"}, _ALL)]  # 0, 1, 2
    d = _judge_trials(best, trials, min_gain=1, require_significant=False)
    assert d.net_gain == 1  # median of {0, 1, 2}, not the best (2)


def test_require_significant_rejects_a_single_question_flip() -> None:
    best = _run(set(), _ALL)
    one_flip = _run({"a"}, _ALL)  # fixed=1, broke=0 → sign-test p=0.5, not significant
    lenient = _judge_trials(best, [one_flip], min_gain=1, require_significant=False)
    assert lenient.keep and not lenient.significant
    strict = _judge_trials(best, [one_flip], min_gain=1, require_significant=True)
    assert not strict.keep  # honest: a 1-question flip is indistinguishable from noise


def test_require_significant_keeps_a_clearly_significant_gain() -> None:
    best = _run(set(), _ALL)
    big = _run({"a", "b", "c", "d", "e"}, _ALL)  # fixed=5, broke=0 → p=0.03125 < 0.05
    d = _judge_trials(best, [big], min_gain=1, require_significant=True)
    assert d.keep and d.significant
