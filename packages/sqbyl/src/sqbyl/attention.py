"""The attention router + readiness scorer (spec §3 #9, §5.5, plan 6.2).

Given a bag of scored :class:`Decision`s, this:

1. **auto-applies** the high-confidence, machine-decidable ones (confidence ≥
   :data:`AUTO_APPLY_THRESHOLD` and not ``requires_human``) — the console shows them
   collapsed/done with one-click undo;
2. **surfaces the rest** into a single queue **sorted by leverage** (highest first — the
   fewest decisions that move readiness the most);
3. computes the **readiness signal**: current accuracy with its interval, and how many queued
   decisions it takes to reach the project's target.

The adapters turn the artifacts sqbyl already produces — a Coach report, an eval's review
pile, an orchestrator run's failed units — into decisions, so the queue is populated from
real work rather than hand-built. Dev-only review machinery, so it lives in ``sqbyl``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqbyl.models.attention import AttentionQueue, Decision, DecisionKind, ReadinessSignal
from sqbyl.stats import wilson_interval

if TYPE_CHECKING:
    from sqbyl.models.coach import CoachReport
    from sqbyl.models.runs import QuestionResult, ScoredRun
    from sqbyl.orchestrator import OrchestratorResult

# Default confidence at/above which a machine decision is applied without asking (one-click
# undo). An unvalidated heuristic, like the judge's DEFAULT_MIN_CONFIDENCE — still needs
# re-deriving against human accept/reject rates once the console logs them. Overridable per
# project via ``DefaultsConfig.auto_apply_threshold`` (set 1.0 to require a human on all).
AUTO_APPLY_THRESHOLD = 0.85


def _leverage_sort_key(d: Decision) -> tuple[float, float, str]:
    """Highest leverage first; ties broken by *lower* confidence (more ambiguous → look sooner),
    then id for a stable order."""
    return (-d.leverage, d.confidence, d.id)


def route(
    decisions: list[Decision],
    *,
    n_correct: int,
    n: int,
    target: float,
    auto_apply_threshold: float = AUTO_APPLY_THRESHOLD,
) -> AttentionQueue:
    """Split ``decisions`` into auto-applied vs. a leverage-sorted queue + a readiness signal.

    ``n_correct``/``n`` are the eval's exact deterministic counts (passed as integers, not a
    reconstructed float, so the interval is honest); ``target`` is the shippable bar from the
    manifest; ``auto_apply_threshold`` gates unsighted apply (the manifest's
    ``auto_apply_threshold`` knob — 1.0 requires a human on everything).
    """

    def auto(d: Decision) -> bool:
        return d.auto_applyable and d.confidence >= auto_apply_threshold

    auto_applied = [d for d in decisions if auto(d)]
    queue = sorted((d for d in decisions if not auto(d)), key=_leverage_sort_key)
    readiness = _readiness(queue, n_correct=n_correct, n=n, target=target)
    return AttentionQueue(auto_applied=auto_applied, queue=queue, readiness=readiness)


def _readiness(queue: list[Decision], *, n_correct: int, n: int, target: float) -> ReadinessSignal:
    """Greedy 'fewest decisions to target': walk the queue high-leverage first, accumulating
    *de-duplicated* estimated leverage until it clears ``target``.

    Leverage is summed across picked decisions, but two decisions that both claim the same
    question can't fix it twice — so a decision's contribution is capped by the fraction of the
    eval its *newly-covered* questions represent (union, not naive sum). The result is a
    projection built on unverified estimates; :class:`ReadinessSignal` labels it as such."""
    accuracy = n_correct / n if n else 0.0
    low, high = wilson_interval(n_correct, n)
    base = ReadinessSignal(
        accuracy=accuracy, target=target, n=n, accuracy_low=low, accuracy_high=high
    )

    if accuracy >= target:
        base.decisions_to_target = 0
        base.projected = accuracy
        base.reachable = True
        return base

    projected = accuracy
    count = 0
    covered: set[str] = set()  # questions already claimed — never credited twice
    for d in queue:
        if projected >= target:
            break
        if d.leverage <= 0.0:
            # A blocked/judge card (leverage 0) is surfaced but never closes the gap —
            # it's absence-of-result, not a gain (the 6.1 contract on failed units).
            continue
        gain = d.leverage
        if d.affected_questions:
            new_q = [q for q in d.affected_questions if q not in covered]
            if not new_q:
                continue  # fully overlaps an earlier pick — contributes nothing new
            gain = min(d.leverage, len(new_q) / n) if n else 0.0
            covered.update(d.affected_questions)
        projected = min(1.0, projected + gain)
        count += 1

    base.decisions_to_target = count
    base.projected = projected
    base.reachable = projected >= target
    return base


# ── adapters: real artifacts → decisions ────────────────────────────────────────────────

_LAYER_TO_KIND: dict[str, DecisionKind] = {
    "example": DecisionKind.example,
    "measure": DecisionKind.measure,
    "synonym": DecisionKind.synonym,
    "named_filter": DecisionKind.named_filter,
    "column_description": DecisionKind.column_description,
    "table_description": DecisionKind.table_description,
    "instruction": DecisionKind.instruction,
    "trusted_asset": DecisionKind.example,
}


def decisions_from_coach_report(report: CoachReport, *, total: int) -> list[Decision]:
    """Turn Coach proposals into decisions. Leverage is ``predicted_fixes / total`` (accuracy
    points), an estimate; prose and memorization-risk proposals are forced to require a human
    (they never auto-apply), consistent with the Coach's own ranking (spec §8)."""
    decisions: list[Decision] = []
    for p in report.proposals:
        leverage = (p.predicted_fixes / total) if total else 0.0
        # Prose (the last-resort layer) and single-question memorization-risk examples never
        # auto-apply — the schema owns "is this prose" via CoachProposal.is_prose (invariant 2).
        requires_human = p.is_prose or p.memorization_risk
        decisions.append(
            Decision(
                id=f"coach:{p.id}",
                kind=_LAYER_TO_KIND.get(p.layer.value, DecisionKind.instruction),
                title=p.title,
                detail=p.root_cause,
                suggestion=p.render_diff(),
                confidence=p.confidence,
                leverage=leverage,
                affected_questions=list(p.question_ids),
                source=f"coach:{p.id}",
                target_file=p.target_file,
                requires_human=requires_human,
            )
        )
    return decisions


def decisions_from_review_pile(run: ScoredRun) -> list[Decision]:
    """Turn an eval's ``manual_review`` rows into judge-review cards — always the human's call.

    These never auto-apply (``requires_human``) and carry no leverage: a review ruling resolves
    a *reported* number, it doesn't teach the agent, so it must not inflate readiness."""
    decisions: list[Decision] = []
    for r in run.results:
        if not r.needs_review:
            continue
        suggestion = r.judge_suggestion.value if r.judge_suggestion is not None else ""
        confidence = _least_judge_confidence(r)
        decisions.append(
            Decision(
                id=f"judge:{run.run_id}:{r.id}",
                kind=DecisionKind.judge_review,
                title=f"Review {r.id}",
                detail=r.question,
                suggestion=suggestion,
                confidence=confidence,
                leverage=0.0,
                affected_questions=[r.id],
                source=f"judge:{r.id}",
                requires_human=True,
            )
        )
    return decisions


def decisions_from_outcomes(result: OrchestratorResult[object]) -> list[Decision]:
    """Turn orchestrator outcomes into cards: a **failed** unit becomes a blocked card the
    human must retry/handle (spec §3 #8) — never auto-applied and carrying no leverage, so it
    surfaces without pretending to be a weak result. Skipped (budget) units aren't cards."""
    decisions: list[Decision] = []
    for o in result.failures:
        decisions.append(
            Decision(
                id=f"blocked:{o.unit.id}",
                kind=DecisionKind.blocked,
                title=o.unit.label or o.unit.id,
                detail=o.error or "unit failed",
                confidence=0.0,
                leverage=0.0,
                source=o.unit.id,
                requires_human=True,
            )
        )
    return decisions


def _least_judge_confidence(result: QuestionResult) -> float:
    """The least-sure judge on a row — a rough 'how ambiguous is this' signal for ordering."""
    if not result.judge_verdicts:
        return 0.0
    return min(v.confidence for v in result.judge_verdicts)
