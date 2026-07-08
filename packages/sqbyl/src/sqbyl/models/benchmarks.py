"""Benchmark questions — ``benchmarks/dev.yaml`` and ``benchmarks/test.yaml`` (spec §4).

Both sets share this shape; the only difference is *who may read them* (the
dev/test code boundary, enforced in Phase 3). Benchmarks are not part of a release,
so this is a dev-only model.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from sqbyl_runtime.models import SqbylModel


class MatchMode(StrEnum):
    """How the deterministic scorer compares a generated result set to gold (spec §7).

    ``exact`` is the honest default: same columns (by position/value), same rows. A weaker
    definition risks scoring a wrong answer correct, so it is never assumed.

    ``columns_superset`` accepts a result that reproduces **every gold column** and **every
    gold row** but carries *extra* informative columns (e.g. gold asks ``name, avg_rating``;
    the agent returns ``product_id, name, avg_rating, review_count``). It is a **weaker**
    correctness bar — the author opts into "extra columns are always fine for this question",
    which is not true universally (an extra ungrouped column can change an aggregate's
    meaning). Opt in per question; keep ``exact`` the default.
    """

    exact = "exact"
    columns_superset = "columns_superset"


class BenchmarkQuestion(SqbylModel):
    """One eval question with its gold answer (SQL or a named trusted asset)."""

    id: str
    question: str
    gold_sql: str | None = None
    # Alternative to gold_sql: name of a trusted asset that defines the answer.
    gold_asset: str | None = None
    # How result_correctness compares this question's rows. Default ``exact``; set
    # ``columns_superset`` to accept correct answers that add extra informative columns
    # (a deliberately weaker bar — see :class:`MatchMode`).
    match_mode: MatchMode = MatchMode.exact
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
