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

from datetime import UTC, datetime

from sqbyl.eval.benchmarks_io import Split
from sqbyl.eval.heldout import load_for_eval
from sqbyl.eval.scorers import score_question
from sqbyl.models import BenchmarkQuestion
from sqbyl.models.runs import QuestionResult, ScoredRun
from sqbyl.project import Project
from sqbyl.projectfiles import load_knowledge, load_trusted_assets
from sqbyl_runtime.context import ProjectKnowledge
from sqbyl_runtime.cost import price_usage
from sqbyl_runtime.db import Database
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
    trace_writer: TraceWriter | None = None,
) -> ScoredRun:
    """Core: score a list of questions with injected deps. See :func:`run_eval` for the
    project-loading convenience wrapper.

    ``as_of`` freezes the clock for ``now()``-relative gold. **Pin it** for a run to be
    reproducible across time; when ``None`` it defaults to run start — internally
    consistent (gold and generated see one instant) but different between runs, so a
    run-diff over unpinned ``now()``-relative questions can attribute clock drift to the
    change under test. The value used is stamped onto the returned :class:`ScoredRun`.
    """
    as_of = as_of or datetime.now(UTC)
    assets = asset_sql or {}
    dialect: Dialect = knowledge.dialect
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
                used_assets=answer.used_assets,
                selected_tables=answer.selected_tables,
                attempts=answer.attempts,
                repaired=answer.repaired,
                error=answer.error,
                usage=answer.usage,
                cost_usd=price_usage(answer.usage, model),
                latency_ms=answer.latency_ms,
                trace_id=answer.trace_id,
            )
        )

    return ScoredRun(
        run_id=new_trace_id(),
        split=split,
        models={"agent": model},
        as_of=as_of,
        results=results,
    )


def run_eval(
    project: Project,
    *,
    split: Split | str,
    llm: LLMClient,
    as_of: datetime | None = None,
    float_tol: float = 1e-6,
    trace_writer: TraceWriter | None = None,
) -> ScoredRun:
    """Run the eval harness over a benchmark ``split`` of ``project``.

    Reads both splits through :func:`sqbyl.eval.heldout.load_for_eval` — eval is the one
    caller allowed to touch the held-out set (invariant 3). Opens the project's read-only
    database and closes it when done.
    """
    split = Split(split)
    questions = load_for_eval(project, split)
    knowledge = load_knowledge(project)
    asset_sql = {a.name: a.sql for a in load_trusted_assets(project)}
    model = project.manifest.model.for_role("agent")
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
            trace_writer=trace_writer,
        )
