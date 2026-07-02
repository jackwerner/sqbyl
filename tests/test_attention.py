"""Phase 6.2 — the attention router + readiness scorer (spec §3 #9, §5.5).

The plan's "done when": given a set of scored decisions, the queue ordering and the readiness
math are unit-tested against expected output. Plus the adapters that turn real artifacts (a
Coach report, an eval review pile, orchestrator failures) into decisions.
"""

from __future__ import annotations

import pytest

from sqbyl.attention import (
    AUTO_APPLY_THRESHOLD,
    decisions_from_coach_report,
    decisions_from_outcomes,
    decisions_from_review_pile,
    route,
)
from sqbyl.models.attention import Decision, DecisionKind
from sqbyl.models.coach import CoachEdit, CoachLayer, CoachProposal, CoachReport
from sqbyl.models.runs import QuestionResult, ScoredRun, Verdict
from sqbyl.orchestrator import Orchestrator, UnitStatus, WorkProduct, WorkUnit
from sqbyl_runtime.llm.base import Usage


def _dec(
    id: str,
    *,
    confidence: float,
    leverage: float = 0.0,
    requires_human: bool = False,
    kind: DecisionKind = DecisionKind.measure,
) -> Decision:
    return Decision(
        id=id,
        kind=kind,
        title=id,
        confidence=confidence,
        leverage=leverage,
        requires_human=requires_human,
    )


# ── auto-apply vs. queue ────────────────────────────────────────────────────────────────


def test_high_confidence_machine_decisions_auto_apply() -> None:
    decisions = [
        _dec("hi", confidence=0.95),  # auto-applied
        _dec("lo", confidence=0.40),  # queued
        _dec("edge", confidence=AUTO_APPLY_THRESHOLD),  # exactly at threshold → auto-applied
    ]
    q = route(decisions, n_correct=40, n=50, target=0.95)

    assert {d.id for d in q.auto_applied} == {"hi", "edge"}
    assert [d.id for d in q.queue] == ["lo"]


def test_requires_human_never_auto_applies_even_at_high_confidence() -> None:
    # A business-meaning card the machine is 99% sure about is still the human's call.
    decisions = [
        _dec("biz", confidence=0.99, requires_human=True, kind=DecisionKind.business_meaning)
    ]
    q = route(decisions, n_correct=40, n=50, target=0.95)

    assert q.auto_applied == []
    assert [d.id for d in q.queue] == ["biz"]


# ── leverage-sorted queue ───────────────────────────────────────────────────────────────


def test_queue_is_sorted_by_leverage_then_ambiguity() -> None:
    decisions = [
        _dec("small", confidence=0.5, leverage=0.02),
        _dec("big", confidence=0.5, leverage=0.10),
        _dec("mid_sure", confidence=0.8, leverage=0.05),
        _dec("mid_unsure", confidence=0.3, leverage=0.05),  # same leverage, more ambiguous → first
    ]
    q = route(decisions, n_correct=40, n=50, target=0.95)

    assert [d.id for d in q.queue] == ["big", "mid_unsure", "mid_sure", "small"]


# ── readiness math ──────────────────────────────────────────────────────────────────────


def test_readiness_counts_fewest_decisions_to_target() -> None:
    # 80% now, target 95% → need +15 points. Highest-leverage-first: 10 + 5 = 15 → 2 decisions.
    decisions = [
        _dec("a", confidence=0.5, leverage=0.10),
        _dec("b", confidence=0.5, leverage=0.05),
        _dec("c", confidence=0.5, leverage=0.05),  # not needed
    ]
    q = route(decisions, n_correct=80, n=100, target=0.95)

    assert q.readiness.decisions_to_target == 2
    assert q.readiness.projected == pytest.approx(0.95)
    assert q.readiness.reachable is True
    assert "~2 decisions to 95%" in q.readiness.headline()


def test_overlapping_decisions_do_not_double_count_toward_target() -> None:
    # Two decisions each claim to fix q1 (0.10 leverage each). Naively summed that's +0.20 and
    # "1 decision to target"; de-duplicated, the shared question can't be fixed twice, so the
    # second adds nothing new and the target stays out of reach on these two alone.
    decisions = [
        Decision(
            id="a",
            kind=DecisionKind.measure,
            title="a",
            confidence=0.5,
            leverage=0.10,
            affected_questions=["q1"],
        ),
        Decision(
            id="b",
            kind=DecisionKind.measure,
            title="b",
            confidence=0.5,
            leverage=0.10,
            affected_questions=["q1"],  # same question — overlap
        ),
    ]
    q = route(decisions, n_correct=80, n=100, target=0.95)

    # Only q1's single point of real coverage counts (1/100 capped), not 0.20.
    assert q.readiness.projected == pytest.approx(0.81)
    assert q.readiness.decisions_to_target == 1  # the second decision contributed nothing new
    assert q.readiness.reachable is False


def test_readiness_flags_unreachable_target() -> None:
    decisions = [_dec("a", confidence=0.5, leverage=0.02)]
    q = route(decisions, n_correct=80, n=100, target=0.95)

    assert q.readiness.reachable is False
    assert q.readiness.projected < 0.95
    assert "not reachable" in q.readiness.headline()


def test_readiness_reached_when_already_at_target() -> None:
    q = route([], n_correct=96, n=100, target=0.95)
    assert q.readiness.reached is True
    assert q.readiness.decisions_to_target == 0
    assert "reached" in q.readiness.headline()


def test_zero_leverage_cards_are_surfaced_but_never_close_the_gap() -> None:
    # A blocked/judge card (leverage 0) must not be counted toward the target — it's
    # absence-of-result, not a gain (the ml-systems contract on failed units).
    decisions = [
        _dec(
            "blocked", confidence=0.0, leverage=0.0, requires_human=True, kind=DecisionKind.blocked
        ),
        _dec("real", confidence=0.5, leverage=0.15),
    ]
    q = route(decisions, n_correct=80, n=100, target=0.95)

    assert q.readiness.decisions_to_target == 1  # only the leverage-bearing one counts
    assert {d.id for d in q.queue} == {"blocked", "real"}


def test_small_eval_set_is_flagged_low_confidence_with_an_interval() -> None:
    q = route([], n_correct=8, n=10, target=0.95)  # n=10 < SMALL_N_FLOOR (30)
    assert q.readiness.low_confidence is True
    assert q.readiness.accuracy_low < 0.8 < q.readiness.accuracy_high  # a real interval attached

    q_big = route([], n_correct=400, n=500, target=0.95)
    assert q_big.readiness.low_confidence is False


# ── adapters: real artifacts → decisions ────────────────────────────────────────────────


def test_coach_report_becomes_leverage_scored_decisions() -> None:
    report = CoachReport(
        run_id="r1",
        proposals=[
            CoachProposal(
                id="p1",
                title="Add net_revenue measure",
                root_cause="model doesn't know refunded rows are excluded",
                layer=CoachLayer.measure,
                target_file="semantics/orders.yaml",
                edits=[CoachEdit(replace="measure net_revenue: ...")],
                predicted_fixes=3,
                confidence=0.9,
                question_ids=["q1", "q2", "q3"],
            ),
            CoachProposal(
                id="p2",
                title="Add a global instruction",
                root_cause="prose",
                layer=CoachLayer.instruction,  # prose → requires human
                target_file="instructions.md",
                edits=[CoachEdit(replace="Always exclude refunds.")],
                predicted_fixes=1,
                confidence=0.95,
                question_ids=["q4"],
            ),
        ],
    )
    decisions = decisions_from_coach_report(report, total=20)

    p1 = next(d for d in decisions if d.id == "coach:p1")
    assert p1.kind is DecisionKind.measure
    assert p1.leverage == 3 / 20
    assert p1.requires_human is False
    assert p1.target_file == "semantics/orders.yaml"

    p2 = next(d for d in decisions if d.id == "coach:p2")
    assert p2.requires_human is True  # prose is never auto-applied

    # Routed: p1 auto-applies (0.9), p2 is queued despite 0.95 (requires human).
    q = route(decisions, n_correct=16, n=20, target=0.95)
    assert {d.id for d in q.auto_applied} == {"coach:p1"}
    assert [d.id for d in q.queue] == ["coach:p2"]


def test_review_pile_becomes_human_only_cards_with_no_leverage() -> None:
    run = ScoredRun(
        run_id="r2",
        split="dev",
        results=[
            QuestionResult(
                id="q1",
                question="How many active customers?",
                generated_sql="SELECT count(*) FROM customers",
                verdict=Verdict.manual_review,
                judge_suggestion=Verdict.correct,
            ),
            QuestionResult(
                id="q2", question="ok", generated_sql="SELECT 1", verdict=Verdict.correct
            ),
        ],
    )
    decisions = decisions_from_review_pile(run)

    assert len(decisions) == 1  # only the manual_review row
    d = decisions[0]
    assert d.kind is DecisionKind.judge_review
    assert d.requires_human is True
    assert d.leverage == 0.0
    assert d.suggestion == "correct"


def test_orchestrator_failures_become_blocked_cards() -> None:
    def boom() -> WorkProduct[str]:
        raise ValueError("cryptic column")

    units: list[WorkUnit[object]] = [
        WorkUnit(id="ok", run=lambda: WorkProduct(value="x", usage=Usage()), label="ok table"),
        WorkUnit(id="bad", run=boom, label="cryptic table"),
    ]
    result = Orchestrator(concurrency=2).run(units)
    decisions = decisions_from_outcomes(result)

    assert len(decisions) == 1
    d = decisions[0]
    assert d.id == "blocked:bad"
    assert d.kind is DecisionKind.blocked
    assert d.requires_human is True
    assert d.leverage == 0.0
    assert "cryptic column" in d.detail
    # And a blocked card never auto-applies.
    q = route(decisions, n_correct=40, n=50, target=0.95)
    assert q.auto_applied == []
    assert [o.status for o in result.failures] == [UnitStatus.failed]
