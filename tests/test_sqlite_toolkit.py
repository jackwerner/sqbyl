"""SQLite must work through the dev toolkit, not just the runtime.

Regression coverage for findings B1/B2 from the BIRD/Spider benchmark experiment:
``introspect`` has no ``information_schema`` to read on SQLite, and ``profile`` has no
in-SQL ``percentile_cont`` — both have to work anyway. CI only exercised DuckDB +
Postgres before, which is exactly why these gaps shipped in 0.4.1.

B7/B8 (found in 0.4.2, once profiling actually ran on BIRD): the profiler emitted
*unquoted* identifiers, so it crashed on real-world names like ``Charter School (Y/N)``;
and the Python percentile path crashed on ``''`` in a dynamically-typed numeric column.
Both aborted the whole ``profile`` command on exactly the messy schemas BIRD is built from.
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


@pytest.fixture
def dirty_sqlite_path(tmp_path: Path) -> Path:
    """A schema shaped like BIRD's ``california_schools``: a table/column name with
    spaces and parentheses, and a numeric column that holds ``''`` for missing."""
    path = tmp_path / "schools.sqlite"
    con = sqlite3.connect(path)
    # Column names carry spaces/parens; "Enrollment (K-12)" is declared numeric but
    # SQLite lets it hold '' (empty string) for a missing value.
    con.execute(
        'CREATE TABLE "Charter Schools" '
        '("School Name" TEXT, "Charter School (Y/N)" INTEGER, "Enrollment (K-12)" REAL)'
    )
    con.executemany(
        'INSERT INTO "Charter Schools" VALUES (?, ?, ?)',
        [("Alpha", 1, 100.0), ("Beta", 0, 200.0), ("Gamma", 1, ""), ("Delta", 0, 300.0)],
    )
    con.commit()
    con.close()
    return path


def test_profile_quotes_identifiers_with_spaces_and_parens(dirty_sqlite_path: Path) -> None:
    # B7: unquoted identifiers crashed the read-only guard's parser on any name with a
    # space or paren. Profiling must run cleanly on the BIRD-shaped schema.
    with Database.connect(str(dirty_sqlite_path), dialect=Dialect.sqlite) as db:
        tables = {t.table: t for t in introspect(db)}
        profiled = profile_table(db, tables["main.Charter Schools"])
    by_name = {c.name: c for c in profiled.columns}

    charter = by_name["Charter School (Y/N)"].profile
    assert charter is not None
    assert charter.min == 0 and charter.max == 1
    assert by_name["Charter School (Y/N)"].sample_values == [0, 1]


def test_profile_tolerates_empty_string_in_numeric_column(dirty_sqlite_path: Path) -> None:
    # B8: '' in a dynamically-typed numeric column must not crash the Python percentile
    # path — it's dropped like the SQL path would, and the real values still profile.
    with Database.connect(str(dirty_sqlite_path), dialect=Dialect.sqlite) as db:
        tables = {t.table: t for t in introspect(db)}
        profiled = profile_table(db, tables["main.Charter Schools"])
    enrollment = {c.name: c for c in profiled.columns}["Enrollment (K-12)"].profile
    assert enrollment is not None
    # Percentiles computed over the three real values [100, 200, 300], '' ignored.
    assert enrollment.p50 == 200.0
