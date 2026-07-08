"""Phase 3.2 — the Layer-1 deterministic scorers against the seeded DuckDB (spec §7).

Covers the done-criteria: known-correct and known-wrong fixtures score correctly, and a
``now()``-relative gold scores **stably** across two different "dates".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from sqbyl.eval.scorers import (
    score_asset_routing,
    score_question,
    score_result_correctness,
    score_schema_accuracy,
    score_syntax_validity,
)
from sqbyl.models.runs import Verdict
from sqbyl_runtime.db import Database
from sqbyl_runtime.models import Dialect

_ORDERS = "analytics.orders"


@pytest.fixture
def db(duckdb_path: Path) -> Iterator[Database]:
    with Database.connect(str(duckdb_path), dialect=Dialect.duckdb) as conn:
        yield conn


# --- syntax_validity ------------------------------------------------------------------


def test_syntax_validity_passes_for_a_single_statement() -> None:
    r = score_syntax_validity(f"SELECT COUNT(*) FROM {_ORDERS}", dialect=Dialect.duckdb)
    assert r.passed is True


def test_syntax_validity_fails_on_garbage() -> None:
    r = score_syntax_validity("SELECT FROM WHERE", dialect=Dialect.duckdb)
    assert r.passed is False


# --- schema_accuracy ------------------------------------------------------------------


def test_schema_accuracy_passes_for_real_columns(db: Database) -> None:
    r = score_schema_accuracy(db, f"SELECT status, amount_cents FROM {_ORDERS}")
    assert r.passed is True


def test_schema_accuracy_catches_hallucinated_column(db: Database) -> None:
    r = score_schema_accuracy(db, f"SELECT nonexistent_col FROM {_ORDERS}")
    assert r.passed is False


# --- asset_routing --------------------------------------------------------------------


def test_asset_routing_not_applicable_without_gold_asset() -> None:
    assert score_asset_routing(gold_asset=None, used_assets=[]).passed is None


def test_asset_routing_pass_and_fail() -> None:
    assert score_asset_routing(gold_asset="mrr", used_assets=["mrr"]).passed is True
    assert score_asset_routing(gold_asset="mrr", used_assets=[]).passed is False


# --- result_correctness ---------------------------------------------------------------


def test_result_correctness_known_correct(db: Database) -> None:
    # Gold and a differently-written-but-equivalent generated query agree.
    gold = f"SELECT COUNT(*) FROM {_ORDERS}"
    generated = f"SELECT COUNT(order_id) AS total FROM {_ORDERS}"
    r = score_result_correctness(db, generated_sql=generated, gold_sql=gold, dialect=Dialect.duckdb)
    assert r.passed is True


def test_result_correctness_known_wrong(db: Database) -> None:
    gold = f"SELECT COUNT(*) FROM {_ORDERS} WHERE status='refunded'"
    generated = f"SELECT COUNT(*) FROM {_ORDERS}"  # counts everything — wrong answer
    r = score_result_correctness(db, generated_sql=generated, gold_sql=gold, dialect=Dialect.duckdb)
    assert r.passed is False


def test_result_correctness_not_applicable_without_gold(db: Database) -> None:
    r = score_result_correctness(
        db, generated_sql=f"SELECT 1 FROM {_ORDERS}", gold_sql=None, dialect=Dialect.duckdb
    )
    assert r.passed is None


def test_result_correctness_generated_sql_that_errors_fails(db: Database) -> None:
    r = score_result_correctness(
        db,
        generated_sql=f"SELECT bogus_col FROM {_ORDERS}",
        gold_sql=f"SELECT COUNT(*) FROM {_ORDERS}",
        dialect=Dialect.duckdb,
    )
    assert r.passed is False


# --- now()-relative stability (gold-SQL drift, §13) -----------------------------------


def test_now_relative_gold_scores_stably_across_dates(db: Database) -> None:
    # A relative-window gold and an equivalent generated query. With the clock frozen to
    # an as_of, the two agree — and the verdict is the same regardless of which date we
    # freeze to (stability), even though the underlying row count genuinely changes.
    gold = f"SELECT COUNT(*) FROM {_ORDERS} WHERE created_at >= now() - INTERVAL 365 DAY"
    generated = f"SELECT COUNT(order_id) FROM {_ORDERS} WHERE created_at > now() - INTERVAL 365 DAY"

    early = datetime(2020, 1, 1)
    late = datetime(2026, 6, 30)
    r_early = score_result_correctness(
        db, generated_sql=generated, gold_sql=gold, as_of=early, dialect=Dialect.duckdb
    )
    r_late = score_result_correctness(
        db, generated_sql=generated, gold_sql=gold, as_of=late, dialect=Dialect.duckdb
    )
    # Stable verdict at both frozen dates...
    assert r_early.passed is True
    assert r_late.passed is True
    # ...and the as_of actually drives execution: the windowed count differs by date.
    from sqbyl.eval.comparator import normalize_as_of

    n_early = db.execute(normalize_as_of(gold, as_of=early, dialect=Dialect.duckdb)).rows[0][0]
    n_late = db.execute(normalize_as_of(gold, as_of=late, dialect=Dialect.duckdb)).rows[0][0]
    assert n_early != n_late


# --- score_question orchestration → Verdict -------------------------------------------


def test_score_question_correct(db: Database) -> None:
    verdict, scorers = score_question(
        db,
        generated_sql=f"SELECT COUNT(*) FROM {_ORDERS}",
        produced_executable_sql=True,
        used_assets=[],
        gold_sql=f"SELECT COUNT(order_id) FROM {_ORDERS}",
        dialect=Dialect.duckdb,
    )
    assert verdict is Verdict.correct
    assert {s.name for s in scorers} == {
        "syntax_validity",
        "schema_accuracy",
        "asset_routing",
        "result_correctness",
    }


def test_score_question_mismatch_routes_to_manual_review(db: Database) -> None:
    # A genuine wrong answer is NOT asserted incorrect by Layer 1 — it's manual review.
    verdict, _ = score_question(
        db,
        generated_sql=f"SELECT COUNT(*) FROM {_ORDERS}",
        produced_executable_sql=True,
        used_assets=[],
        gold_sql=f"SELECT COUNT(*) FROM {_ORDERS} WHERE status='refunded'",
        dialect=Dialect.duckdb,
    )
    assert verdict is Verdict.manual_review


def test_score_question_superset_mode_credits_extra_columns(db: Database) -> None:
    # Gold asks for the distinct statuses; the agent returns them WITH a count column.
    # In exact mode that mismatch is manual_review; in columns_superset it's correct.
    from sqbyl.models.benchmarks import MatchMode

    gold = f"SELECT DISTINCT status FROM {_ORDERS}"
    generated = f"SELECT status, COUNT(*) AS n FROM {_ORDERS} GROUP BY status"

    exact_verdict, _ = score_question(
        db,
        generated_sql=generated,
        produced_executable_sql=True,
        used_assets=[],
        gold_sql=gold,
        dialect=Dialect.duckdb,
    )
    assert exact_verdict is Verdict.manual_review

    superset_verdict, _ = score_question(
        db,
        generated_sql=generated,
        produced_executable_sql=True,
        used_assets=[],
        gold_sql=gold,
        dialect=Dialect.duckdb,
        match_mode=MatchMode.columns_superset,
    )
    assert superset_verdict is Verdict.correct


def test_score_question_error_when_no_executable_sql(db: Database) -> None:
    verdict, _ = score_question(
        db,
        generated_sql=f"SELECT bogus_col FROM {_ORDERS}",
        produced_executable_sql=False,
        used_assets=[],
        gold_sql=f"SELECT COUNT(*) FROM {_ORDERS}",
        dialect=Dialect.duckdb,
    )
    assert verdict is Verdict.error
