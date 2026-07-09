"""SQLite must work through the dev toolkit, not just the runtime.

Regression coverage for findings B1/B2 from the BIRD/Spider benchmark experiment:
``introspect`` has no ``information_schema`` to read on SQLite, and ``profile`` has no
in-SQL ``percentile_cont`` — both have to work anyway. CI only exercised DuckDB +
Postgres before, which is exactly why these gaps shipped in 0.4.1.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sqbyl.introspect import introspect
from sqbyl.profile import _percentile_cont, profile_table
from sqbyl_runtime.db import Database
from sqbyl_runtime.models import Dialect


@pytest.fixture
def sqlite_path(tmp_path: Path) -> Path:
    path = tmp_path / "shop.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, category TEXT, price INTEGER)")
    con.executemany(
        "INSERT INTO products (category, price) VALUES (?, ?)",
        [("a", 10), ("a", 20), ("b", 30), ("b", 40), ("c", 50)],
    )
    con.commit()
    con.close()
    return path


def test_introspect_reads_sqlite_schema(sqlite_path: Path) -> None:
    # B1: discovery must fall back to the SQLAlchemy inspector, not information_schema.
    with Database.connect(str(sqlite_path), dialect=Dialect.sqlite) as db:
        tables = {t.table: t for t in introspect(db)}
    assert "main.products" in tables
    assert {c.name for c in tables["main.products"].columns} >= {"id", "category", "price"}


def test_profile_computes_percentiles_in_python_on_sqlite(sqlite_path: Path) -> None:
    # B2: no percentile_cont on SQLite, so the profiler computes quantiles in Python.
    with Database.connect(str(sqlite_path), dialect=Dialect.sqlite) as db:
        tables = {t.table: t for t in introspect(db)}
        profiled = profile_table(db, tables["main.products"])
    by_name = {c.name: c for c in profiled.columns}

    price = by_name["price"].profile
    assert price is not None
    assert price.min == 10 and price.max == 50
    assert price.distinct == 5
    # percentile_cont over [10, 20, 30, 40, 50]: exact ranks, no interpolation.
    assert price.p25 == 20.0 and price.p50 == 30.0 and price.p75 == 40.0

    # The value-grounding that BIRD's whole thesis rides on still works on SQLite:
    # a low-cardinality column captures its top-k values for the prompt.
    assert by_name["category"].sample_values == ["a", "b", "c"]


def test_percentile_cont_interpolates() -> None:
    # Matches SQL percentile_cont: interpolate between the two nearest ranks.
    assert _percentile_cont([10.0, 20.0, 30.0, 40.0], 0.5) == 25.0
    assert _percentile_cont([7.0], 0.9) == 7.0
    assert _percentile_cont([], 0.5) is None
