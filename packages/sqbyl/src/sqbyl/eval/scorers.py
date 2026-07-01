"""Layer-1 deterministic scorers (spec §7 Layer 1, plan 3.2).

Cheap, objective, and run **always** — the primary eval signal, before any LLM judge.
Four scorers:

* ``syntax_validity`` — does the generated SQL parse as a single statement?
* ``schema_accuracy`` — do all referenced tables/columns exist? (``EXPLAIN`` binds the
  statement against the live schema, catching hallucinated columns without executing it)
* ``asset_routing`` — when a trusted asset *should* have answered, did the agent cite it?
* ``result_correctness`` — execute gold and generated SQL and compare result sets. The
  headline objective scorer.

:func:`score_question` orchestrates them into one :class:`Verdict`. A result-set
**mismatch is routed to ``manual_review``, never asserted "incorrect"** — Layer 1 alone
can't prove two queries disagree on meaning (spec §7); Layer-2 judges / a human resolve
those in later phases.
"""

from __future__ import annotations

from datetime import datetime

import sqlglot
from sqlglot.errors import SqlglotError

from sqbyl.eval.comparator import compare_result_sets, normalize_as_of
from sqbyl.models.runs import (
    SCORER_ASSET_ROUTING,
    SCORER_RESULT_CORRECTNESS,
    SCORER_SCHEMA_ACCURACY,
    SCORER_SYNTAX_VALIDITY,
    ScorerResult,
    Verdict,
)
from sqbyl_runtime.db import (
    Database,
    StaticValidationError,
    UnparseableSqlError,
    WriteAttemptError,
)
from sqbyl_runtime.models import Dialect


def _sqlglot_dialect(dialect: Dialect) -> str:
    return "postgres" if dialect is Dialect.postgresql else dialect.value


def score_syntax_validity(sql: str, *, dialect: Dialect) -> ScorerResult:
    """Does the generated SQL parse cleanly as exactly one statement? (No DB needed.)"""
    try:
        parsed = sqlglot.parse(sql, read=_sqlglot_dialect(dialect))
        statements = [s for s in parsed if s is not None]
    except SqlglotError as exc:
        return ScorerResult(name=SCORER_SYNTAX_VALIDITY, passed=False, detail=f"parse error: {exc}")
    if len(statements) != 1:
        return ScorerResult(
            name=SCORER_SYNTAX_VALIDITY,
            passed=False,
            detail=f"expected a single statement, parsed {len(statements)}",
        )
    return ScorerResult(name=SCORER_SYNTAX_VALIDITY, passed=True, detail="parses as one statement")


def score_schema_accuracy(db: Database, sql: str) -> ScorerResult:
    """Do all referenced tables/columns resolve against the live schema?

    ``EXPLAIN`` binds the statement without running it, so a hallucinated column or
    table fails here (spec §5 step 4).
    """
    try:
        db.explain(sql)
    except (StaticValidationError, WriteAttemptError, UnparseableSqlError) as exc:
        return ScorerResult(name=SCORER_SCHEMA_ACCURACY, passed=False, detail=str(exc))
    return ScorerResult(
        name=SCORER_SCHEMA_ACCURACY, passed=True, detail="all referenced tables/columns resolve"
    )


def score_asset_routing(*, gold_asset: str | None, used_assets: list[str]) -> ScorerResult:
    """When a trusted asset is the gold answer, did the agent actually cite it?

    Returns ``passed=None`` (not applicable) for questions whose gold is raw SQL — asset
    routing is only meaningful when a trusted asset *should* have answered (spec §7).
    """
    if gold_asset is None:
        return ScorerResult(
            name=SCORER_ASSET_ROUTING, passed=None, detail="no gold asset for this question"
        )
    if gold_asset in used_assets:
        return ScorerResult(
            name=SCORER_ASSET_ROUTING, passed=True, detail=f"cited trusted asset {gold_asset!r}"
        )
    return ScorerResult(
        name=SCORER_ASSET_ROUTING,
        passed=False,
        detail=f"expected the agent to use trusted asset {gold_asset!r}; it cited {used_assets}",
    )


def score_result_correctness(
    db: Database,
    *,
    generated_sql: str,
    gold_sql: str | None,
    as_of: datetime | None = None,
    dialect: Dialect,
    float_tol: float = 1e-6,
) -> ScorerResult:
    """Execute gold and generated SQL and compare result sets (the headline scorer).

    ``passed=None`` (not applicable) when there is no executable gold SQL — that case is
    routed to manual review by :func:`score_question`. Both statements are ``as_of``-
    normalized so a ``now()``-relative gold scores stably (spec §13).
    """
    if gold_sql is None:
        return ScorerResult(
            name=SCORER_RESULT_CORRECTNESS, passed=None, detail="no executable gold SQL"
        )
    try:
        gold_rows = db.execute(normalize_as_of(gold_sql, as_of=as_of, dialect=dialect))
    except Exception as exc:  # noqa: BLE001 — a broken gold is a benchmark-authoring problem
        return ScorerResult(
            name=SCORER_RESULT_CORRECTNESS, passed=None, detail=f"gold SQL failed to execute: {exc}"
        )
    try:
        gen_rows = db.execute(normalize_as_of(generated_sql, as_of=as_of, dialect=dialect))
    except Exception as exc:  # noqa: BLE001 — generated SQL that won't run is a clear miss
        return ScorerResult(
            name=SCORER_RESULT_CORRECTNESS,
            passed=False,
            detail=f"generated SQL failed to execute: {exc}",
        )
    comparison = compare_result_sets(gold_rows, gen_rows, float_tol=float_tol)
    return ScorerResult(
        name=SCORER_RESULT_CORRECTNESS, passed=comparison.equal, detail=comparison.reason
    )


def score_question(
    db: Database,
    *,
    generated_sql: str,
    produced_executable_sql: bool,
    used_assets: list[str],
    gold_sql: str | None = None,
    gold_asset_name: str | None = None,
    gold_asset_sql: str | None = None,
    as_of: datetime | None = None,
    dialect: Dialect,
    float_tol: float = 1e-6,
) -> tuple[Verdict, list[ScorerResult]]:
    """Run every applicable Layer-1 scorer and fold them into one :class:`Verdict`.

    ``produced_executable_sql`` is the agent's own success flag: if the pipeline never
    landed runnable SQL (all self-repairs failed), the verdict is ``error`` regardless of
    the static scorers, which still run for diagnostics. Otherwise the verdict follows
    ``result_correctness``: pass → ``correct``; mismatch or no gold → ``manual_review``.
    A ``gold_asset`` is resolved to its SQL (``gold_asset_sql``) for the correctness
    comparison when available.
    """
    scorers: list[ScorerResult] = [
        score_syntax_validity(generated_sql, dialect=dialect),
        score_schema_accuracy(db, generated_sql),
        score_asset_routing(gold_asset=gold_asset_name, used_assets=used_assets),
    ]
    effective_gold = gold_sql if gold_sql is not None else gold_asset_sql
    correctness = score_result_correctness(
        db,
        generated_sql=generated_sql,
        gold_sql=effective_gold,
        as_of=as_of,
        dialect=dialect,
        float_tol=float_tol,
    )
    scorers.append(correctness)

    if not produced_executable_sql:
        return Verdict.error, scorers
    if correctness.passed is True:
        return Verdict.correct, scorers
    # Mismatch, or no gold to compare against → defer to Layer 2 / a human (spec §7).
    return Verdict.manual_review, scorers
