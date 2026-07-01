"""The execution-grounded synthesizer — ``sqbyl synth`` (spec §6.A, plan 4.1).

The cold-start helper that makes a golden set buildable in an afternoon. Three steps:

1. **Seed** ($0, deterministic): walk the semantic layer and turn every table, measure,
   named filter, and join into *question fodder*, stratified by difficulty. Seeds route
   the model's attention at real business logic instead of trivia (spec's
   examples-first hierarchy).
2. **Draft** (paid, one structured call): Claude proposes candidate questions with gold
   SQL, plus a phrasing variant or two per canonical question. Metered like every paid
   command (invariant 5).
3. **Ground** ($0, deterministic): execute every gold SQL against the real database and
   **discard anything that errors, returns nothing, or is degenerate** — so a human only
   ever reviews questions whose answer already ran. Survivors carry the executed rows as
   evidence for the review console (spec §6.5).

Survivors land in the review queue and, once accepted, in the **dev** set only — this
module has no path to the held-out ``test.yaml`` (invariant 3), enforced by an
import-linter contract (it must not import :mod:`sqbyl.eval.heldout`).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from sqbyl.candidates_io import load_candidates
from sqbyl.eval.benchmarks_io import _read_dev_set_lenient
from sqbyl.eval.comparator import normalize_as_of
from sqbyl.models import (
    Candidate,
    DroppedCandidate,
    DropReason,
    ExecutionEvidence,
    SynthResult,
)
from sqbyl.project import Project
from sqbyl.projectfiles import load_semantics
from sqbyl_runtime.db import Database, QueryResult
from sqbyl_runtime.db.errors import (
    StaticValidationError,
    UnparseableSqlError,
    WriteAttemptError,
)
from sqbyl_runtime.llm.base import LLMClient, LLMRequest, Message, Usage
from sqbyl_runtime.models import Dialect, TableSemantics
from sqbyl_runtime.state.traces import TraceWriter, llm_call_span, new_trace_id

_SYSTEM = (
    "You author evaluation questions for a text-to-SQL benchmark. For each question you "
    "also write the GOLD SQL that answers it. Rules: (1) every query must be a single "
    "read-only SELECT in the given SQL dialect; (2) use the fully-qualified table names "
    "and real columns from the schema — never invent columns; (3) each gold query must "
    "actually return rows against real data (avoid impossible filters); (4) exercise the "
    "provided measures, named filters, and joins — that is the business logic worth "
    "testing; (5) for some canonical questions, add one or two phrasing VARIANTS that ask "
    "the same thing differently, sharing a `group` slug with their canonical question and "
    "setting canonical=false. Stratify difficulty across easy/medium/hard."
)


class DraftQuestion(BaseModel):
    """One model-proposed question + gold SQL, before it has been executed."""

    question: str
    gold_sql: str
    difficulty: str | None = None
    canonical: bool = True
    # A slug shared by a canonical question and its phrasing variants, used to link them.
    group: str | None = None
    # Which seed this exercises, echoed back for coverage reporting (free-form).
    seed: str | None = None


class DraftBatch(BaseModel):
    """The structured result of the single paid drafting call."""

    questions: list[DraftQuestion] = Field(default_factory=list)


class Seed(BaseModel):
    """One unit of $0 question fodder derived from the semantic layer."""

    label: str
    difficulty: str
    hint: str


def plan_seeds(tables: list[TableSemantics]) -> list[Seed]:
    """Turn the semantic layer into difficulty-stratified question fodder ($0).

    A table seeds simple single-table aggregates; a measure or named filter seeds a
    medium question that must use that business definition; a join seeds a hard,
    cross-table breakdown.
    """
    seeds: list[Seed] = []
    for table in tables:
        seeds.append(
            Seed(
                label=f"table:{table.table}",
                difficulty="easy",
                hint=f"a simple count or single-table aggregate over {table.table}",
            )
        )
        for measure in table.measures:
            seeds.append(
                Seed(
                    label=f"measure:{table.table}.{measure.name}",
                    difficulty="medium",
                    hint=f"use the measure {measure.name} := {measure.sql}",
                )
            )
        for filt in table.filters:
            seeds.append(
                Seed(
                    label=f"filter:{table.table}.{filt.name}",
                    difficulty="medium",
                    hint=f"apply the named filter {filt.name} := {filt.sql}",
                )
            )
        for join in table.joins:
            seeds.append(
                Seed(
                    label=f"join:{table.table}->{join.to}",
                    difficulty="hard",
                    hint=f"a breakdown that joins {table.table} to {join.to} on {join.on}",
                )
            )
    return seeds


def _render_schema(tables: list[TableSemantics]) -> str:
    lines: list[str] = []
    for table in tables:
        lines.append(f"Table: {table.table}")
        if table.description:
            lines.append(f"  {table.description}")
        for col in table.columns:
            desc = f" — {col.description}" if col.description else ""
            lines.append(f"  - {col.name} ({col.type}){desc}")
        for measure in table.measures:
            lines.append(f"  measure {measure.name}: {measure.sql}")
        for filt in table.filters:
            lines.append(f"  filter {filt.name}: {filt.sql}")
        for join in table.joins:
            lines.append(f"  join -> {join.to} ON {join.on}")
        lines.append("")
    return "\n".join(lines)


def _render_prompt(
    tables: list[TableSemantics], seeds: list[Seed], *, n: int, dialect: Dialect
) -> str:
    seed_lines = "\n".join(f"- [{s.difficulty}] {s.label}: {s.hint}" for s in seeds)
    return (
        f"SQL dialect: {dialect.value}\n\n"
        f"SCHEMA:\n{_render_schema(tables)}\n"
        f"COVERAGE SEEDS (aim to exercise these):\n{seed_lines}\n\n"
        f"Produce about {n} candidate questions with gold SQL, spread across the seeds and "
        f"difficulty levels, including a few phrasing variants. Return them all."
    )


def draft_candidates(
    llm: LLMClient,
    *,
    tables: list[TableSemantics],
    seeds: list[Seed],
    model: str,
    n: int,
    dialect: Dialect,
    trace_writer: TraceWriter | None = None,
    trace_id: str | None = None,
    parent_span_id: str | None = None,
) -> tuple[list[DraftQuestion], Usage]:
    """The single paid call: ask the model for ``~n`` candidates covering the seeds.

    Built as an explicit request so the token-spending call is written as an OTel-GenAI
    span when a ``trace_writer`` is given (invariant 7).
    """
    request = LLMRequest(
        model=model,
        messages=[
            Message(role="user", content=_render_prompt(tables, seeds, n=n, dialect=dialect))
        ],
        system=_SYSTEM,
        response_schema=DraftBatch.model_json_schema(),
        max_tokens=8192,
        temperature=0.0,
        cache_system=True,
    )
    response = llm.complete(request)
    if trace_writer is not None:
        trace_writer.write(
            llm_call_span(
                request,
                response,
                operation="chat",
                name="synth draft",
                trace_id=trace_id or new_trace_id(),
                parent_span_id=parent_span_id,
            )
        )
    return response.parse(DraftBatch).questions, response.usage


def _slug(question: str, *, taken: set[str]) -> str:
    """A stable, unique ``q_...`` id from the question text."""
    base = re.sub(r"[^a-z0-9]+", "_", question.lower()).strip("_")[:48] or "candidate"
    candidate = f"q_{base}"
    i = 2
    while candidate in taken:
        candidate = f"q_{base}_{i}"
        i += 1
    taken.add(candidate)
    return candidate


def _is_degenerate(result: QueryResult) -> bool:
    """A non-empty result with nothing to assert on — every cell is NULL."""
    return all(cell is None for row in result.rows for cell in row)


def ground_candidates(
    db: Database,
    drafts: list[DraftQuestion],
    *,
    dialect: Dialect,
    as_of: datetime | None = None,
    reserved_ids: set[str] | None = None,
) -> tuple[list[Candidate], list[DroppedCandidate]]:
    """Execute every draft's gold SQL and keep only the ones that really run ($0).

    A draft is dropped when its SQL fails static validation / is not a pure read, raises
    on execution, returns zero rows, or returns only NULLs. Survivors get a stable id and
    the executed rows as review evidence. The same ``as_of`` freezes ``now()``-relative
    gold so a relative-window candidate executes deterministically (spec §13).

    ``reserved_ids`` are ids already in use elsewhere (a prior run's queue, the dev set):
    the slugger avoids them so two *different* questions synthesized in separate runs can't
    collide onto one id and silently overwrite each other downstream.
    """
    survivors: list[Candidate] = []
    dropped: list[DroppedCandidate] = []
    taken: set[str] = set(reserved_ids or ())
    group_to_canonical: dict[str, str] = {}

    for draft in drafts:
        evidence, reason, detail = check_gold_sql(db, draft.gold_sql, dialect=dialect, as_of=as_of)
        if evidence is None:
            assert reason is not None  # no evidence ⇒ a drop reason
            dropped.append(
                DroppedCandidate(
                    question=draft.question,
                    gold_sql=draft.gold_sql,
                    reason=reason,
                    detail=detail,
                )
            )
            continue
        cid = _slug(draft.question, taken=taken)
        candidate = Candidate(
            id=cid,
            question=draft.question,
            gold_sql=draft.gold_sql,
            difficulty=draft.difficulty,
            canonical=draft.canonical,
            seed=draft.seed,
            evidence=evidence,
        )
        if draft.group:
            if draft.canonical or draft.group not in group_to_canonical:
                group_to_canonical.setdefault(draft.group, cid)
            elif not draft.canonical:
                candidate = candidate.model_copy(
                    update={"variant_of": group_to_canonical[draft.group]}
                )
        survivors.append(candidate)

    return survivors, dropped


def check_gold_sql(
    db: Database,
    gold_sql: str,
    *,
    dialect: Dialect,
    as_of: datetime | None = None,
) -> tuple[ExecutionEvidence | None, DropReason | None, str | None]:
    """Execution-ground one gold query. Returns ``(evidence, drop_reason, detail)``.

    On a keeper the evidence is present and the reason is ``None``; on a drop the evidence
    is ``None`` and the reason explains why. Shared by :func:`ground_candidates` (synthesis)
    and the review console's edit-and-re-run-live feature (spec §6.5).
    """
    sql = normalize_as_of(gold_sql, as_of=as_of, dialect=dialect)
    reason, detail, result = _execute_for_grounding(db, sql)
    if reason is not None:
        return None, reason, detail
    assert result is not None
    return ExecutionEvidence.from_result(result), None, None


def _execute_for_grounding(
    db: Database, sql: str
) -> tuple[DropReason | None, str | None, QueryResult | None]:
    """Validate + run one gold query. Returns ``(drop_reason, detail, result)``.

    ``drop_reason`` is ``None`` on a keeper; otherwise ``result`` is ``None``.
    """
    try:
        db.explain(sql)
    except (StaticValidationError, WriteAttemptError, UnparseableSqlError) as exc:
        return DropReason.syntax_error, str(exc), None
    except Exception as exc:  # an unexpected connection/driver failure during EXPLAIN
        # Never let a grounding failure escape as an unhandled 500 in the console; a query
        # we couldn't validate is simply not a keeper.
        return DropReason.execution_error, f"could not validate: {exc}", None
    try:
        result = db.execute(sql)
    except Exception as exc:  # a driver error EXPLAIN didn't surface
        return DropReason.execution_error, str(exc), None
    if not result.rows:
        return DropReason.empty_result, "gold SQL returned zero rows", None
    if _is_degenerate(result):
        return DropReason.degenerate, "gold SQL returned only NULLs", None
    return None, None, result


def synthesize(
    project: Project,
    *,
    llm: LLMClient,
    model: str,
    n: int = 20,
    as_of: datetime | None = None,
    trace_writer: TraceWriter | None = None,
) -> SynthResult:
    """Seed → draft → ground over ``project`` → a :class:`SynthResult`.

    Opens the project's read-only database for grounding and closes it when done. Does
    **not** write the queue or meter usage — that's the caller's job (the CLI), keeping
    this pure and testable under record-replay with an injected client (invariant 4).
    """
    as_of = as_of or datetime.now(UTC)
    tables = load_semantics(project)
    seeds = plan_seeds(tables)
    dialect = project.manifest.database.dialect
    trace_id = new_trace_id()
    drafts, usage = draft_candidates(
        llm,
        tables=tables,
        seeds=seeds,
        model=model,
        n=n,
        dialect=dialect,
        trace_writer=trace_writer,
        trace_id=trace_id,
    )
    # Reserve ids already used by a prior run's queue and by the dev set, so a fresh run's
    # slugs can't collide with — and silently clobber — existing candidates/questions.
    reserved = {c.id for c in load_candidates(project)} | {
        q.id for q in _read_dev_set_lenient(project)
    }
    with project.connect() as db:
        survivors, dropped = ground_candidates(
            db, drafts, dialect=dialect, as_of=as_of, reserved_ids=reserved
        )
    return SynthResult(survivors=survivors, dropped=dropped, usage=usage)
