"""Phase 9.4 — importers: dbt/query-log/view SQL → proposed examples + joins (plan 9.4).

Deterministic and $0 (no LLM). Join extraction is unit-tested on SQL strings; the corpus
import is exercised end-to-end against the seeded DuckDB fixture (execution-grounding,
candidate + join production, drop reasons); statement splitting and view introspection
round-trip against real SQL.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sqbyl.importers import (
    QueryInput,
    extract_joins,
    import_queries,
    queries_from_dbt,
    queries_from_views,
    split_sql_statements,
)
from sqbyl_runtime.db import Database
from sqbyl_runtime.models import Dialect

_DUCK = Dialect.duckdb


# --- join extraction -------------------------------------------------------------


def test_extract_joins_reads_the_on_edge() -> None:
    sql = "SELECT * FROM analytics.orders o JOIN analytics.customers c ON o.customer_id = c.id"
    joins = extract_joins(sql, dialect=_DUCK)
    assert len(joins) == 1
    j = joins[0]
    assert {j.from_table, j.to_table} == {"analytics.orders", "analytics.customers"}
    assert "customer_id" in j.on
    assert j.confidence < 0.9  # observed-only, not FK-grade


def test_extract_joins_handles_multiple_joins() -> None:
    sql = "SELECT * FROM a JOIN b ON a.x = b.x JOIN c ON b.y = c.y"
    joins = extract_joins(sql, dialect=_DUCK)
    pairs = {frozenset((j.from_table, j.to_table)) for j in joins}
    assert pairs == {frozenset({"a", "b"}), frozenset({"b", "c"})}


def test_extract_joins_ignores_a_join_without_qualified_columns() -> None:
    # A cross join / no ON, or unqualified columns, yields no confident edge.
    assert extract_joins("SELECT * FROM a, b", dialect=_DUCK) == []


def test_extract_joins_fails_soft_on_garbage() -> None:
    assert extract_joins("this is not sql )(", dialect=_DUCK) == []


# --- corpus import (execution-grounded) ------------------------------------------


@pytest.fixture
def db(duckdb_path: Path) -> Database:
    return Database.connect(str(duckdb_path), dialect=Dialect.duckdb)


def test_import_queries_grounds_and_builds_candidates(db: Database) -> None:
    inputs = [
        QueryInput(sql="SELECT COUNT(*) AS n FROM analytics.orders", label="order count"),
        QueryInput(
            sql=(
                "SELECT c.customer_id FROM analytics.orders o "
                "JOIN analytics.customers c ON o.customer_id = c.customer_id"
            ),
            source="query-log",
        ),
    ]
    result = import_queries(inputs, db=db, dialect=Dialect.duckdb)
    assert result.n_candidates == 2
    # The labeled one got a real question seed; the unlabeled one is flagged for the human.
    labeled = next(c for c in result.candidates if "order" in c.question.lower())
    assert "import" in labeled.tags
    unlabeled = next(c for c in result.candidates if "needs-question" in c.tags)
    assert unlabeled.question.startswith("[needs question]")
    # The join in the second query is proposed.
    assert result.n_joins == 1
    db.close()


def test_label_derived_question_is_tagged_derived(db: Database) -> None:
    inputs = [QueryInput(sql="SELECT COUNT(*) FROM analytics.orders", label="order count")]
    result = import_queries(inputs, db=db, dialect=Dialect.duckdb)
    # A name-derived question is honestly marked as inferred, not presented as authored.
    assert "derived-question" in result.candidates[0].tags
    db.close()


def test_literal_carrying_sql_is_flagged(db: Database) -> None:
    # A query-log statement with a baked-in literal (possibly PII) is tagged so review
    # surfaces it before it can cross into the committed dev.yaml.
    inputs = [
        QueryInput(sql="SELECT * FROM analytics.customers WHERE region = 'emea'", label="emea"),
        QueryInput(sql="SELECT COUNT(*) FROM analytics.orders", label="count"),
    ]
    result = import_queries(inputs, db=db, dialect=Dialect.duckdb)
    literal = next(c for c in result.candidates if "emea" in c.gold_sql)
    plain = next(c for c in result.candidates if "COUNT" in c.gold_sql)
    assert "contains-literals" in literal.tags
    assert "contains-literals" not in plain.tags
    db.close()


def test_import_drops_a_query_that_does_not_run(db: Database) -> None:
    inputs = [QueryInput(sql="SELECT nope FROM analytics.orders", label="bad")]
    result = import_queries(inputs, db=db, dialect=Dialect.duckdb)
    assert result.n_candidates == 0
    assert len(result.dropped) == 1
    db.close()


def test_import_drops_an_empty_result(db: Database) -> None:
    inputs = [QueryInput(sql="SELECT * FROM analytics.orders WHERE 1=0", label="empty")]
    result = import_queries(inputs, db=db, dialect=Dialect.duckdb)
    assert result.n_candidates == 0
    assert result.dropped[0].reason.value == "empty_result"
    db.close()


def test_write_sql_is_dropped_not_run(db: Database) -> None:
    # A write smuggled into the corpus is refused by the read-only guard, never executed.
    inputs = [QueryInput(sql="DELETE FROM analytics.orders", label="danger")]
    result = import_queries(inputs, db=db, dialect=Dialect.duckdb)
    assert result.n_candidates == 0 and len(result.dropped) == 1
    assert db.execute("SELECT COUNT(*) FROM analytics.orders").rows[0][0] == 2000
    db.close()


def test_data_modifying_cte_is_dropped_not_run(db: Database) -> None:
    # A write smuggled inside a SELECT-looking CTE is caught by the guard, not executed —
    # the exact shape a hostile query log would take (guard.py is built for this).
    inputs = [
        QueryInput(
            sql="WITH d AS (DELETE FROM analytics.orders RETURNING *) SELECT * FROM d",
            label="sneaky",
        )
    ]
    result = import_queries(inputs, db=db, dialect=Dialect.duckdb)
    assert result.n_candidates == 0 and len(result.dropped) == 1
    assert db.execute("SELECT COUNT(*) FROM analytics.orders").rows[0][0] == 2000


def test_unparseable_query_is_soft_dropped_not_raised(db: Database) -> None:
    # An unparseable statement (e.g. a dialect-specific view body) must drop, not crash the run.
    inputs = [
        QueryInput(sql="SELECT FROM WHERE )(", label="junk"),
        QueryInput(sql="SELECT COUNT(*) FROM analytics.orders", label="good"),
    ]
    result = import_queries(inputs, db=db, dialect=Dialect.duckdb)
    assert result.n_candidates == 1  # the good one survived; the junk didn't take the run down
    assert len(result.dropped) == 1
    db.close()


def test_repeated_join_accumulates_confidence(db: Database) -> None:
    join_sql = (
        "SELECT c.customer_id FROM analytics.orders o "
        "JOIN analytics.customers c ON o.customer_id = c.customer_id"
    )
    inputs = [QueryInput(sql=join_sql, label=f"q{i}") for i in range(3)]
    result = import_queries(inputs, db=db, dialect=Dialect.duckdb)
    assert result.n_joins == 1  # deduped across the three queries
    assert result.joins[0].hits == 3
    assert result.joins[0].confidence > 0.4  # frequency bumped it above the base
    db.close()


# --- sources ---------------------------------------------------------------------


def test_split_sql_statements_keeps_only_reads_and_lifts_comments() -> None:
    log = (
        "-- daily revenue\nSELECT SUM(amount_cents) FROM analytics.orders;\n"
        "INSERT INTO analytics.orders VALUES (1);\n"  # a write in the log is ignored
        "SELECT 1;\n"
    )
    inputs = split_sql_statements(log, dialect=_DUCK)
    assert len(inputs) == 2  # the INSERT is dropped
    assert inputs[0].label == "daily revenue"  # the leading comment became the label


def test_queries_from_dbt_reads_compiled_sql(tmp_path: Path) -> None:
    compiled = tmp_path / "target" / "compiled" / "proj" / "models"
    compiled.mkdir(parents=True)
    (compiled / "revenue_by_day.sql").write_text(
        "SELECT date, SUM(amount_cents) FROM analytics.orders GROUP BY date"
    )
    inputs = queries_from_dbt(tmp_path / "target" / "compiled", dialect=_DUCK)
    assert len(inputs) == 1
    assert inputs[0].label == "revenue_by_day"
    assert inputs[0].source == "dbt:revenue_by_day"


def test_queries_from_views_reads_view_definitions(duckdb_path: Path, tmp_path: Path) -> None:
    import duckdb

    # A throwaway copy of the fixture with a view added — read its definition back out.
    dst = tmp_path / "with_view.duckdb"
    con = duckdb.connect(str(dst))
    con.execute("CREATE SCHEMA analytics")
    con.execute("CREATE TABLE analytics.orders (id INT, amount_cents INT)")
    con.execute("INSERT INTO analytics.orders VALUES (1, 500), (2, 700)")
    con.execute(
        "CREATE VIEW analytics.revenue AS SELECT SUM(amount_cents) AS r FROM analytics.orders"
    )
    con.close()

    with Database.connect(str(dst), dialect=Dialect.duckdb) as db:
        inputs = queries_from_views(db)
    names = {i.label for i in inputs}
    assert "revenue" in names
