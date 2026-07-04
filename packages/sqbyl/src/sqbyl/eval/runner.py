"""The eval runner (spec §7, plan 3.1).

Runs each benchmark question as a **fresh, stateless** ``ask()`` conversation — no thread
context carries between questions — then scores it with the Layer-1 deterministic scorers
and folds the results into one :class:`ScoredRun`. The run is stamped with the agent model
version and the frozen ``as_of`` (so it's reproducible and a score is never divorced from
the model that produced it, spec §7).

The runner does **not** write to ``.sqbyl/`` — persistence and metering are the caller's
job (:mod:`sqbyl.eval.report`, the CLI) — so it stays pure and testable under
record-replay with an injected LLM client (invariant 4).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqbyl.calibration_io import few_shot_examples
from sqbyl.eval.benchmarks_io import Split
from sqbyl.eval.heldout import load_for_eval
from sqbyl.eval.judges import (
    DEFAULT_MIN_CONFIDENCE,
    ArbiterOutcome,
    adjudicate,
    load_judge_prompts,
)
from sqbyl.eval.scorers import score_question
from sqbyl.models import BenchmarkQuestion, CalibrationRecord
from sqbyl.models.runs import QuestionResult, ScoredRun
from sqbyl.project import Project
from sqbyl.projectfiles import load_knowledge, load_trusted_assets
from sqbyl_runtime.context import ProjectKnowledge
from sqbyl_runtime.cost import price_usage
from sqbyl_runtime.db import Database
from sqbyl_runtime.fingerprint import fingerprint_knowledge, live_schema_fingerprint
from sqbyl_runtime.llm.base import LLMClient
from sqbyl_runtime.models import Dialect
from sqbyl_runtime.pipeline import ask
from sqbyl_runtime.state.traces import TraceWriter, new_trace_id


def score_run(
    questions: list[BenchmarkQuestion],
    *,
    db: Database,
    knowledge: ProjectKnowledge,
    llm: LLMClient,
    model: str,
    split: str,
    asset_sql: dict[str, str] | None = None,
    as_of: datetime | None = None,
    self_repair_attempts: int = 2,
    float_tol: float = 1e-6,
    judge_llm: LLMClient | None = None,
    judge_model: str | None = None,
    judge_prompts: dict[str, str] | None = None,
    judge_min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    judge_examples: list[CalibrationRecord] | None = None,
    trace_writer: TraceWriter | None = None,
) -> ScoredRun:
    """Core: score a list of questions with injected deps. See :func:`run_eval` for the
    project-loading convenience wrapper.

    ``as_of`` freezes the clock for ``now()``-relative gold. **Pin it** for a run to be
    reproducible across time; when ``None`` it defaults to run start — internally
    consistent (gold and generated see one instant) but different between runs, so a
    run-diff over unpinned ``now()``-relative questions can attribute clock drift to the
    change under test. The value used is stamped onto the returned :class:`ScoredRun`.

    When ``judge_llm`` is given, Layer-2 judges (spec §7) adjudicate every ``manual_review``
    row through :func:`sqbyl.eval.judges.adjudicate`; ``correct``/``error`` rows short-
    circuit and cost zero tokens. Left ``None``, only the deterministic Layer 1 runs (the
    caller decides via ``automation.auto_judge``). The judge client is a separate seam from
    the agent ``llm`` so the two can be scripted independently — in production both resolve
    to the same key, per-role model (spec §9).
    """
    as_of = as_of or datetime.now(UTC)
    assets = asset_sql or {}
    dialect: Dialect = knowledge.dialect
    judging = judge_llm is not None
    if judging and (judge_model is None or judge_prompts is None):
        raise ValueError("judge_model and judge_prompts are required when judge_llm is given")
    results: list[QuestionResult] = []

    for q in questions:
        # Fresh, stateless ask() per question — no cross-question thread context (spec §7).
        answer = ask(
            q.question,
            knowledge=knowledge,
            db=db,
            llm=llm,
            model=model,
            self_repair_attempts=self_repair_attempts,
            trace_writer=trace_writer,
        )
        effective_gold = (
            q.gold_sql
            if q.gold_sql is not None
            else (assets.get(q.gold_asset) if q.gold_asset else None)
        )
        verdict, scorers = score_question(
            db,
            generated_sql=answer.sql,
            produced_executable_sql=answer.ok,
            used_assets=answer.used_assets,
            gold_sql=q.gold_sql,
            gold_asset_name=q.gold_asset,
            gold_asset_sql=assets.get(q.gold_asset) if q.gold_asset else None,
            as_of=as_of,
            dialect=dialect,
            float_tol=float_tol,
        )
        # Layer 2: adjudicate only the rows Layer 1 couldn't resolve. adjudicate short-
        # circuits (zero tokens) on any verdict that isn't manual_review (spec §7).
        outcome = ArbiterOutcome(suggestion=None)
        if judge_llm is not None:
            assert judge_model is not None and judge_prompts is not None  # narrowed above
            outcome = adjudicate(
                judge_llm,
                verdict=verdict,
                question=q.question,
                generated_sql=answer.sql,
                gold_sql=effective_gold,
                prompts=judge_prompts,
                dialect=dialect,
                model=judge_model,
                min_confidence=judge_min_confidence,
                examples=judge_examples,
                trace_writer=trace_writer,
                trace_id=answer.trace_id,
            )
        # Cost is attributed per model: agent tokens at the agent price, judge tokens at the
        # judge price (a possibly-different pinned model, spec §9) — kept on separate fields.
        judge_cost = (
            price_usage(outcome.usage, judge_model)
            if outcome.usage.total_tokens and judge_model is not None
            else 0.0
        )
        results.append(
            QuestionResult(
                id=q.id,
                question=q.question,
                verdict=verdict,
                generated_sql=answer.sql,
                plan=answer.plan,
                gold_sql=q.gold_sql,
                gold_asset=q.gold_asset,
                scorers=scorers,
                judge_verdicts=outcome.judge_verdicts,
                judge_suggestion=outcome.suggestion,
                used_assets=answer.used_assets,
                selected_tables=answer.selected_tables,
                attempts=answer.attempts,
                repaired=answer.repaired,
                error=answer.error,
                usage=answer.usage,
                cost_usd=price_usage(answer.usage, model),
                judge_usage=outcome.usage,
                judge_cost_usd=judge_cost,
                latency_ms=answer.latency_ms,
                trace_id=answer.trace_id,
            )
        )

    models = {"agent": model}
    if judging and judge_model is not None:
        models["judge"] = judge_model
    return ScoredRun(
        run_id=new_trace_id(),
        split=split,
        models=models,
        as_of=as_of,
        judge_calibration=_calibration_fingerprint(judge_examples),
        # Tie this run to the exact brain that produced it, so a release can refuse to
        # stamp a held-out score against files that have since changed (spec §11).
        knowledge_fingerprint=fingerprint_knowledge(knowledge),
        # And to the live DB it scored, so load() can warn if production's schema drifted.
        schema_fingerprint=live_schema_fingerprint(db, knowledge.semantics),
        results=results,
    )


def _calibration_fingerprint(examples: list[CalibrationRecord] | None) -> str | None:
    """A stable fingerprint of the few-shot anchors that coached the judge this run, so a
    judged run is reproducible from its stamped inputs (spec §7/§11). ``None`` when the judge
    was un-coached."""
    if not examples:
        return None
    key = "\n".join(f"{e.question_id}:{e.human_verdict.value}:{e.note}" for e in examples)
    return f"n={len(examples)},sha={hashlib.sha256(key.encode()).hexdigest()[:12]}"


def run_eval(
    project: Project,
    *,
    split: Split | str,
    llm: LLMClient,
    as_of: datetime | None = None,
    float_tol: float = 1e-6,
    judge: bool | None = None,
    trace_writer: TraceWriter | None = None,
) -> ScoredRun:
    """Run the eval harness over a benchmark ``split`` of ``project``.

    Reads both splits through :func:`sqbyl.eval.heldout.load_for_eval` — eval is the one
    caller allowed to touch the held-out set (invariant 3). Opens the project's read-only
    database and closes it when done.

    Layer-2 judging (spec §7) follows ``automation.auto_judge`` by default; pass
    ``judge=True``/``False`` to force it on or off (e.g. a ``--no-judge`` run). When on, the
    same ``llm`` runs the judge role at the project's pinned ``judge`` model.
    """
    split = Split(split)
    questions = load_for_eval(project, split)
    knowledge = load_knowledge(project)
    asset_sql = {a.name: a.sql for a in load_trusted_assets(project)}
    model = project.manifest.model.for_role("agent")
    judge_on = project.manifest.automation.auto_judge if judge is None else judge
    with project.connect() as db:
        return score_run(
            questions,
            db=db,
            knowledge=knowledge,
            llm=llm,
            model=model,
            split=split.value,
            asset_sql=asset_sql,
            as_of=as_of,
            self_repair_attempts=project.manifest.defaults.self_repair_attempts,
            float_tol=float_tol,
            judge_llm=llm if judge_on else None,
            judge_model=project.manifest.model.for_role("judge") if judge_on else None,
            judge_prompts=load_judge_prompts(project) if judge_on else None,
            # Prior human rulings coach the judge (spec §7) — but only on **dev**. The
            # held-out test judge stays pristine (no few-shot) so the measurement instrument
            # doesn't drift, and dev rulings can never leak into test judging (invariant 3).
            # Empty until someone reviews, so a fresh project's prompts (and CI cassettes)
            # are unchanged.
            judge_examples=(
                few_shot_examples(project, split=split.value)
                if judge_on and split is Split.dev
                else None
            ),
            trace_writer=trace_writer,
        )
