"""Benchmark questions — ``benchmarks/dev.yaml`` and ``benchmarks/test.yaml`` (spec §4).

Both sets share this shape; the only difference is *who may read them* (the
dev/test code boundary, enforced in Phase 3). Benchmarks are not part of a release,
so this is a dev-only model.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from sqbyl_runtime.models import SqbylModel


class BenchmarkQuestion(SqbylModel):
    """One eval question with its gold answer (SQL or a named trusted asset)."""

    id: str
    question: str
    gold_sql: str | None = None
    # Alternative to gold_sql: name of a trusted asset that defines the answer.
    gold_asset: str | None = None
    eval_note: str | None = None
    difficulty: str | None = None
    tags: list[str] = Field(default_factory=list)
    # Phrasing variants share meaning with a canonical question; the console marks
    # which is canonical. Canonical by default.
    canonical: bool = True

    @model_validator(mode="after")
    def _exactly_one_gold(self) -> BenchmarkQuestion:
        has_sql = self.gold_sql is not None
        has_asset = self.gold_asset is not None
        if has_sql == has_asset:
            raise ValueError(
                f"question {self.id!r} must set exactly one of 'gold_sql' or 'gold_asset'"
            )
        return self
