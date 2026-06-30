"""Phase 1.2 — schema introspector (spec §3 #1, §1.2).

Exit criteria: introspecting the fixture reproduces its known schema into models,
with FK-derived joins; an FK-less database yields low-confidence heuristic joins.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from sqbyl.introspect import introspect
from sqbyl_runtime.db import Database
from sqbyl_runtime.models import Dialect


def test_introspect_fixture_schema(duckdb_path: Path) -> None:
    with Database.connect(str(duckdb_path), dialect=Dialect.duckdb) as db:
        tables = {t.table: t for t in introspect(db)}

    assert set(tables) == {"analytics.orders", "analytics.customers"}

    orders = tables["analytics.orders"]
    types = {c.name: c.type for c in orders.columns}
    assert types == {
        "order_id": "bigint",
        "customer_id": "bigint",
        "amount_cents": "bigint",
        "status": "varchar",
        "created_at": "timestamp",
    }
    # Introspection leaves *meaning* blank — descriptions are the annotator's job.
    assert all(c.description is None for c in orders.columns)
    assert orders.description is None
    # Profiling has not run yet.
    assert all(c.profile is None for c in orders.columns)


def test_fk_becomes_high_confidence_join(duckdb_path: Path) -> None:
    with Database.connect(str(duckdb_path), dialect=Dialect.duckdb) as db:
        orders = next(t for t in introspect(db) if t.table == "analytics.orders")
    assert len(orders.joins) == 1
    join = orders.joins[0]
    assert join.to == "analytics.customers"
    assert join.type == "many_to_one"
    assert join.on == "orders.customer_id = customers.customer_id"
    assert join.confidence == 1.0


def test_heuristic_joins_for_fkless_db(tmp_path: Path) -> None:
    db_path = tmp_path / "nofk.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE dept (dept_id BIGINT PRIMARY KEY, name TEXT)")
    # emp.dept_id matches dept's PK by name + type, but there is no FK declared.
    con.execute("CREATE TABLE emp (emp_id BIGINT PRIMARY KEY, dept_id BIGINT, name TEXT)")
    con.close()

    with Database.connect(str(db_path), dialect=Dialect.duckdb) as db:
        emp = next(t for t in introspect(db) if t.table.endswith("emp"))

    assert len(emp.joins) == 1
    join = emp.joins[0]
    assert join.to.endswith("dept")
    assert join.on == "emp.dept_id = dept.dept_id"
    # A candidate, not a fact: low confidence so the attention router surfaces it.
    assert join.confidence is not None and join.confidence < 1.0
