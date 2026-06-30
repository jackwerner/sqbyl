"""Semantic-layer models — one ``TableSemantics`` per table/view (spec §4).

These describe the meaning of the schema: column descriptions, deterministic
``profile:`` stats, joins, measures, and filters. They are embedded verbatim in a
release artifact, so they live in ``sqbyl-runtime``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from sqbyl_runtime.models.base import SqbylModel

# A profile min/max can be numeric or an ISO date/timestamp string (e.g. a
# created_at column's coverage window), so the bound is a small scalar union.
ScalarBound = int | float | str


class Profile(SqbylModel):
    """Deterministic, $0 column statistics written by the profiler (spec §3.1).

    Every field is optional: the profiler degrades to cheaper stats on large
    tables (sampling), and PII columns may suppress values entirely.
    """

    nulls: float | None = Field(default=None, ge=0.0, le=1.0, description="Null fraction (0–1).")
    distinct: int | None = Field(default=None, ge=0, description="Approximate distinct count.")
    min: ScalarBound | None = None
    max: ScalarBound | None = None
    p25: float | None = None
    p50: float | None = None
    p75: float | None = None
    p95: float | None = None
    sampled: bool = Field(
        default=False,
        description="True if computed over a sample (TABLESAMPLE/row cap), not a full scan.",
    )


class Column(SqbylModel):
    """A single column's type + meaning + grounding stats."""

    name: str
    type: str
    description: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    # Top-k representative values (from the profile); powers lexical value-matching.
    # Suppressed (None) for PII even when the rest of the profile is kept.
    sample_values: list[ScalarBound] | None = None
    profile: Profile | None = None


JoinCardinality = Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]


class Join(SqbylModel):
    """A join path to another table. FK-derived joins are high confidence;
    name/type-heuristic candidates are emitted as low-confidence stubs (spec §1.2)."""

    to: str
    type: JoinCardinality
    on: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class Measure(SqbylModel):
    """A named, reusable aggregate expression (e.g. net_revenue)."""

    name: str
    description: str | None = None
    sql: str


class Filter(SqbylModel):
    """A named, reusable WHERE fragment (e.g. last_quarter)."""

    name: str
    description: str | None = None
    sql: str


class TableSemantics(SqbylModel):
    """The full semantic description of one table/view (``semantics/<table>.yaml``)."""

    table: str
    description: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    columns: list[Column] = Field(default_factory=list)
    joins: list[Join] = Field(default_factory=list)
    measures: list[Measure] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
