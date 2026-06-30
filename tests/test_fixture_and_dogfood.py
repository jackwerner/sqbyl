"""Phase 0.4 — the seeded DuckDB opens and the dogfood project deserializes cleanly.

Exit criterion: tests can open the fixture DB and read its schema, and the example
sqbyl project round-trips into the Phase 0.2 models with zero external deps.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from sqbyl.models import BenchmarkQuestion, SqbylManifest
from sqbyl.yamlio import load_yaml
from sqbyl_runtime.models import Example, TableSemantics


def test_fixture_schema_is_readable(duckdb_path: Path) -> None:
    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        tables = {
            row[0]
            for row in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='analytics'"
            ).fetchall()
        }
        assert {"orders", "customers"} <= tables
        n_orders = con.execute("SELECT COUNT(*) FROM analytics.orders").fetchone()[0]
        n_customers = con.execute("SELECT COUNT(*) FROM analytics.customers").fetchone()[0]
        assert n_orders == 2000
        assert n_customers == 200
        # The data is realistic enough to profile: three order statuses, cents range.
        rows = con.execute("SELECT DISTINCT status FROM analytics.orders").fetchall()
        assert {r[0] for r in rows} == {"confirmed", "refunded", "partial_refund"}
    finally:
        con.close()


def test_fixture_rebuild_is_deterministic(tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_orders_duckdb",
        Path(__file__).resolve().parents[1] / "fixtures" / "build_orders_duckdb.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def net_revenue(path: Path) -> float:
        module.build(path)
        con = duckdb.connect(str(path), read_only=True)
        try:
            val = con.execute(
                "SELECT SUM(CASE WHEN status='confirmed' THEN amount_cents ELSE 0 END)/100.0 "
                "FROM analytics.orders"
            ).fetchone()[0]
        finally:
            con.close()
        return round(val, 2)

    # Same seed → same data → same aggregate, build after build.
    assert net_revenue(tmp_path / "a.duckdb") == net_revenue(tmp_path / "b.duckdb")


def test_dogfood_manifest_deserializes(dogfood_dir: Path) -> None:
    manifest = SqbylManifest.model_validate(load_yaml((dogfood_dir / "sqbyl.yaml").read_text()))
    assert manifest.name == "revenue-analytics"
    assert manifest.database.dialect == "duckdb"
    assert manifest.model.for_role("agent") == "claude-opus-4-8"


def test_dogfood_semantics_deserialize(dogfood_dir: Path) -> None:
    sem_dir = dogfood_dir / "semantics"
    tables = {}
    for path in sorted(sem_dir.glob("*.yaml")):
        table = TableSemantics.model_validate(load_yaml(path.read_text()))
        tables[table.table] = table
    assert set(tables) == {"analytics.orders", "analytics.customers"}
    orders = tables["analytics.orders"]
    # The net_revenue measure and the customers join are present and typed.
    assert any(m.name == "net_revenue" for m in orders.measures)
    assert orders.joins[0].to == "analytics.customers"
    status_col = next(c for c in orders.columns if c.name == "status")
    assert status_col.sample_values == ["confirmed", "partial_refund", "refunded"]


def test_dogfood_examples_deserialize(dogfood_dir: Path) -> None:
    raw = load_yaml((dogfood_dir / "examples" / "revenue.yaml").read_text())
    examples = [Example.model_validate(item) for item in raw]
    assert len(examples) == 3
    assert all(ex.sql.strip().upper().startswith("SELECT") for ex in examples)


def test_dogfood_benchmarks_deserialize(dogfood_dir: Path) -> None:
    for name, expected in [("dev.yaml", 5), ("test.yaml", 3)]:
        raw = load_yaml((dogfood_dir / "benchmarks" / name).read_text())
        questions = [BenchmarkQuestion.model_validate(item) for item in raw]
        assert len(questions) == expected
        # Every question has exactly one gold (model invariant), and ids are unique.
        assert len({q.id for q in questions}) == len(questions)
        assert all((q.gold_sql is not None) ^ (q.gold_asset is not None) for q in questions)
