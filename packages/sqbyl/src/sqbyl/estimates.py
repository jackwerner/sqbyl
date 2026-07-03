"""Per-command cost estimates — the "here's the plan" of `sqbyl init` (spec §5.5, §9).

Every paid command shows an up-front, itemized estimate before spending, and returns
that same estimate (spending nothing) under ``--dry-run`` / ``sqbyl cost``. This module
is the single place the token assumptions behind those estimates live, so the number a
command *prints* and the number ``cost`` *reports* can never drift — and so the guessed
per-call token sizes are reviewable in one spot rather than scattered as magic numbers
across the CLI.

The numbers lean deliberately *conservative* so the estimate never under-reads the bill
(the whole point of "no surprise charge", spec §1.5): no prompt-cache savings are assumed,
the agent line includes the worst-case **self-repair** retries a question can trigger, and
the eval judge line budgets the full multi-judge **panel** per review row. Real metered
spend should therefore land at or under the quote — cache hits and questions that pass on
the first try only make it cheaper.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqbyl.models.judges import GOLD_MISMATCH_JUDGES
from sqbyl_runtime.cost import CostEstimate, EstimateItem

if TYPE_CHECKING:
    from sqbyl.project import Project

# Rough per-call token sizes by workload. Inputs are dominated by the compiled
# schema/semantics block; outputs by the structured result the role emits.
_ASK_IN, _ASK_OUT = 1500, 300
_ANNOTATE_IN, _ANNOTATE_OUT = 1500, 400
_SYNTH_IN, _SYNTH_OUT = 2000, 2000  # one batch drafting call, large output
_COACH_IN, _COACH_OUT = 4000, 2000  # failures + full context in, ranked diffs out
_JUDGE_IN, _JUDGE_OUT = 1200, 250  # per judge, per review-pile row
# The standard judge panel size (semantic-equivalence + logical-accuracy + completeness).
# A review row runs the whole panel, so the eval estimate budgets the panel, not one call.
_JUDGE_PANEL = len(GOLD_MISMATCH_JUDGES)


def _count_tables(project: Project) -> int:
    return len(sorted(project.semantics_dir.glob("*.yaml")))


def _count_dev_questions(project: Project) -> int:
    # Dev-only: the estimator must not reach the held-out door (invariant 3). `sqbyl eval`
    # sizes its estimate off the dev count; test-split estimates would over/under-read
    # slightly, which is acceptable for an estimate and keeps this module dev-safe.
    from sqbyl.eval.benchmarks_io import dev_set_size

    return dev_set_size(project)


def ask_estimate(model: str, *, self_repair_attempts: int = 0) -> CostEstimate:
    # A question can retry up to ``self_repair_attempts`` times, each a fresh paid call, so
    # the ceiling is 1 + attempts calls (spec §3 self-repair; metered in pipeline.ask).
    calls = 1 + self_repair_attempts
    return CostEstimate(
        items=[
            EstimateItem(
                label=f"answer 1 question (≤{calls} calls incl. self-repair)",
                model=model,
                calls=calls,
                avg_input_tokens=_ASK_IN,
                avg_output_tokens=_ASK_OUT,
            )
        ]
    )


def annotate_estimate(model: str, *, tables: int) -> CostEstimate:
    return CostEstimate(
        items=[
            EstimateItem(
                label=f"annotate {tables} table(s)",
                model=model,
                calls=tables,
                avg_input_tokens=_ANNOTATE_IN,
                avg_output_tokens=_ANNOTATE_OUT,
            )
        ]
    )


def synth_estimate(model: str, *, n: int) -> CostEstimate:
    return CostEstimate(
        items=[
            EstimateItem(
                label=f"synthesize ~{n} candidate question(s)",
                model=model,
                calls=1,
                avg_input_tokens=_SYNTH_IN,
                avg_output_tokens=_SYNTH_OUT,
            )
        ]
    )


def coach_estimate(model: str, *, failures: int) -> CostEstimate:
    return CostEstimate(
        items=[
            EstimateItem(
                label=f"coach {failures} dev failure(s)",
                model=model,
                calls=1,
                avg_input_tokens=_COACH_IN,
                avg_output_tokens=_COACH_OUT,
            )
        ]
    )


def eval_estimate(
    agent_model: str,
    *,
    questions: int,
    judge_model: str | None = None,
    self_repair_attempts: int = 0,
) -> CostEstimate:
    """Agent calls (with self-repair headroom) plus, when judging is on, the judge panel.

    Both lines are conservative ceilings so the estimate never *under*-reads: the agent line
    assumes every question may exhaust its self-repair retries, and the judge line assumes
    every question lands in the review pile and runs the whole ``_JUDGE_PANEL``. In practice
    most questions pass on the first try and skip judging entirely, so real spend is lower.
    """
    per_question_calls = 1 + self_repair_attempts
    items = [
        EstimateItem(
            label=f"eval {questions} question(s) (≤{per_question_calls} calls each w/ self-repair)",
            model=agent_model,
            calls=questions * per_question_calls,
            avg_input_tokens=_ASK_IN,
            avg_output_tokens=_ASK_OUT,
        )
    ]
    if judge_model is not None:
        items.append(
            EstimateItem(
                label=f"judge ≤{questions} review row(s) × {_JUDGE_PANEL}-judge panel",
                model=judge_model,
                calls=questions * _JUDGE_PANEL,
                avg_input_tokens=_JUDGE_IN,
                avg_output_tokens=_JUDGE_OUT,
            )
        )
    return CostEstimate(items=items)


def estimate_for_command(project: Project, command: str, *, n: int = 20) -> CostEstimate:
    """Route ``sqbyl cost <command>`` / ``--dry-run`` to the matching planner.

    Uses project-derived counts (tables on disk, dev/test question counts) so the estimate
    reflects the real workload without spending anything. Raises ``KeyError`` for a command
    that isn't paid (there's no estimate to give).
    """
    manifest = project.manifest.model
    repairs = project.manifest.defaults.self_repair_attempts
    if command == "ask":
        return ask_estimate(manifest.for_role("agent"), self_repair_attempts=repairs)
    if command == "annotate":
        return annotate_estimate(manifest.default, tables=_count_tables(project))
    if command == "synth":
        return synth_estimate(manifest.for_role("synth"), n=n)
    if command == "eval":
        judge = manifest.for_role("judge") if project.manifest.automation.auto_judge else None
        return eval_estimate(
            manifest.for_role("agent"),
            questions=_count_dev_questions(project),
            judge_model=judge,
            self_repair_attempts=repairs,
        )
    if command == "coach":
        return coach_estimate(manifest.for_role("coach"), failures=_count_failures(project))
    raise KeyError(command)


def _count_failures(project: Project) -> int:
    """Failures in the latest dev run (what `coach` would work on); 0 if there's no run."""
    from sqbyl.coach import gather_failures
    from sqbyl.eval.report import latest_run
    from sqbyl_runtime.state.layout import SqbylPaths

    run = latest_run(SqbylPaths(project.root), split="dev")
    return len(gather_failures(run)) if run is not None else 0
