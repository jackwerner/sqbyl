"""Synthesizer candidates — the review queue between ``synth`` and the dev set (spec §6.A).

``sqbyl synth`` drafts candidate questions with gold SQL, **executes** each one, and keeps
only those whose SQL actually ran and returned a non-degenerate result. A survivor is a
:class:`Candidate`: a proposed :class:`~sqbyl.models.BenchmarkQuestion` plus the execution
evidence (columns + a sample of the rows it returned) the review console shows so a human
does a fast yes/no pass instead of authoring from scratch.

These are dev-only models — a candidate is never part of a release, and the queue is
``.sqbyl/`` scratch state. Accepted candidates flow to ``benchmarks/dev.yaml`` only; a
candidate has no path to the held-out ``test.yaml`` (invariant 3).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import Field

from sqbyl.models.benchmarks import BenchmarkQuestion
from sqbyl_runtime.db import QueryResult
from sqbyl_runtime.llm.base import Usage
from sqbyl_runtime.models import SqbylModel

# A sane cap on how many rows of evidence we keep per candidate — enough for a human to
# eyeball that the answer is sensible without bloating the queue file.
EVIDENCE_ROW_CAP = 20


class CandidateStatus(StrEnum):
    """Where a candidate is in the review flow. ``pending`` until a human decides."""

    pending = "pending"
    accepted = "accepted"
    rejected = "rejected"


class DropReason(StrEnum):
    """Why the synthesizer discarded a candidate before it ever reached review.

    Execution-grounding (spec §6.A) means a human only sees questions whose gold SQL
    already ran and returned a real answer; everything else is dropped with one of these.
    """

    syntax_error = "syntax_error"  # failed static EXPLAIN / didn't parse
    execution_error = "execution_error"  # EXPLAIN passed but running it raised
    empty_result = "empty_result"  # ran fine, returned zero rows
    degenerate = "degenerate"  # a single all-NULL cell — nothing to assert on


def _coerce_cell(value: object) -> Any:
    """Coerce one result cell to a JSON/YAML-native scalar for the evidence sample.

    Datetimes and decimals become strings so the queue file and the console's JSON stay
    portable; primitives pass through unchanged.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


class ExecutionEvidence(SqbylModel):
    """Proof a candidate's gold SQL runs: its columns and a capped sample of rows.

    ``row_count`` is the *full* count returned; ``rows`` may be truncated to
    :data:`EVIDENCE_ROW_CAP` so the queue file stays small.
    """

    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = Field(ge=0)

    @classmethod
    def from_result(
        cls, result: QueryResult, *, row_cap: int = EVIDENCE_ROW_CAP
    ) -> ExecutionEvidence:
        return cls(
            columns=list(result.columns),
            rows=[[_coerce_cell(v) for v in row] for row in result.rows[:row_cap]],
            row_count=len(result.rows),
        )


class Candidate(SqbylModel):
    """A synthesized question that has been executed and is awaiting human review."""

    id: str
    question: str
    gold_sql: str
    difficulty: str | None = None
    tags: list[str] = Field(default_factory=list)
    # Phrasing variants share meaning with a canonical question (Genie-style phrasing
    # variation, spec §6.A). ``variant_of`` names the canonical candidate's id.
    canonical: bool = True
    variant_of: str | None = None
    # What semantic-layer element seeded this candidate, e.g. ``"measure:net_revenue"`` —
    # kept so review can see (and the synthesizer can report) coverage of real business logic.
    seed: str | None = None
    evidence: ExecutionEvidence
    status: CandidateStatus = CandidateStatus.pending

    def to_question(self) -> BenchmarkQuestion:
        """The durable benchmark shape written to ``dev.yaml`` on accept (drops evidence)."""
        return BenchmarkQuestion(
            id=self.id,
            question=self.question,
            gold_sql=self.gold_sql,
            difficulty=self.difficulty,
            tags=list(self.tags),
            canonical=self.canonical,
        )


class DroppedCandidate(SqbylModel):
    """A candidate the synthesizer discarded, kept only for the run's drop report."""

    question: str
    gold_sql: str
    reason: DropReason
    detail: str | None = None


class SynthResult(SqbylModel):
    """The outcome of one ``synth`` run: survivors, drops, and metered usage.

    ``survivors`` are the executed, non-degenerate candidates written to the review queue;
    ``dropped`` records what execution-grounding rejected and why (so the drop rate is
    visible, not silent). ``usage`` aggregates the paid drafting calls for metering.
    """

    survivors: list[Candidate] = Field(default_factory=list)
    dropped: list[DroppedCandidate] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)

    @property
    def n_survivors(self) -> int:
        return len(self.survivors)

    @property
    def n_dropped(self) -> int:
        return len(self.dropped)

    @property
    def n_drafted(self) -> int:
        return self.n_survivors + self.n_dropped
