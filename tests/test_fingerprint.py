"""Phase 8.2 review hardening — the live schema fingerprint's drift properties (spec §11).

The load-time schema-mismatch check must catch the real footgun (a renamed/dropped/altered
*declared* table) without crying wolf on changes the agent can't see. These are focused unit
tests of :func:`sqbyl_runtime.fingerprint.live_schema_fingerprint` against tiny live DuckDBs:

* an **additive** migration (a new column the brain never declared) must NOT drift it;
* a **dropped/renamed declared** column MUST drift it;
* a pure **type-rendering** difference (``text`` vs ``varchar``) must NOT drift it;
* **column-name case** must not drift it.
"""

from __future__ import annotations

from pathlib import Path

from sqbyl_runtime.db import Database
from sqbyl_runtime.fingerprint import drifted_tables, live_schema_fingerprint
from sqbyl_runtime.models import Column, Dialect, TableSemantics

_SEM = [
    TableSemantics(
        table="main.t",
        columns=[Column(name="a", type="int"), Column(name="b", type="varchar")],
    )
]


def _db(tmp_path: Path, name: str, ddl: str) -> Database:
    path = tmp_path / f"{name}.duckdb"
    import duckdb

    con = duckdb.connect(str(path))
    con.execute(ddl)
    con.close()
    return Database.connect(str(path), dialect=Dialect.duckdb, read_only=True)


def test_additive_migration_does_not_drift(tmp_path: Path) -> None:
    base = _db(tmp_path, "base", "CREATE TABLE t (a INTEGER, b VARCHAR)")
    added = _db(tmp_path, "added", "CREATE TABLE t (a INTEGER, b VARCHAR, c INTEGER)")
    # A new column the brain never declared is invisible to the agent → same fingerprint.
    assert live_schema_fingerprint(base, _SEM) == live_schema_fingerprint(added, _SEM)
    assert drifted_tables(added, _SEM) == []
    base.close()
    added.close()


def test_dropped_declared_column_drifts(tmp_path: Path) -> None:
    base = _db(tmp_path, "base", "CREATE TABLE t (a INTEGER, b VARCHAR)")
    dropped = _db(tmp_path, "dropped", "CREATE TABLE t (a INTEGER)")  # declared `b` is gone
    assert live_schema_fingerprint(base, _SEM) != live_schema_fingerprint(dropped, _SEM)
    assert drifted_tables(dropped, _SEM) == ["main.t"]
    base.close()
    dropped.close()


def test_missing_table_drifts_and_is_named(tmp_path: Path) -> None:
    gone = _db(tmp_path, "gone", "CREATE TABLE other (x INTEGER)")  # no `t` at all
    base = _db(tmp_path, "base2", "CREATE TABLE t (a INTEGER, b VARCHAR)")
    assert live_schema_fingerprint(gone, _SEM) != live_schema_fingerprint(base, _SEM)
    assert drifted_tables(gone, _SEM) == ["main.t"]
    gone.close()
    base.close()


def test_type_rendering_difference_does_not_drift(tmp_path: Path) -> None:
    # DuckDB renders both TEXT and VARCHAR as VARCHAR, but even where a driver differs the
    # string family is collapsed — the demonstrated `text` vs `varchar` false positive.
    varchar = _db(tmp_path, "vc", "CREATE TABLE t (a INTEGER, b VARCHAR)")
    text = _db(tmp_path, "txt", "CREATE TABLE t (a INTEGER, b TEXT)")
    assert live_schema_fingerprint(varchar, _SEM) == live_schema_fingerprint(text, _SEM)
    varchar.close()
    text.close()


def test_column_name_case_does_not_drift(tmp_path: Path) -> None:
    # A brain that declares `A`/`B` against a DuckDB that reports `a`/`b` is not real drift.
    sem = [
        TableSemantics(
            table="main.t",
            columns=[Column(name="A", type="int"), Column(name="B", type="varchar")],
        )
    ]
    db = _db(tmp_path, "case", "CREATE TABLE t (a INTEGER, b VARCHAR)")
    base = _db(tmp_path, "case_base", "CREATE TABLE t (a INTEGER, b VARCHAR)")
    assert live_schema_fingerprint(db, sem) == live_schema_fingerprint(base, _SEM)
    assert drifted_tables(db, sem) == []
    db.close()
    base.close()


def test_altered_declared_type_family_drifts_the_fingerprint(tmp_path: Path) -> None:
    # A genuine cross-family type change (varchar → integer) is real drift the fingerprint must
    # catch. `drifted_tables` is presence-based, so it won't *name* a pure type change (that
    # needs the eval-time baseline, not the YAML type) — the aggregate warning still fires.
    base = _db(tmp_path, "b3", "CREATE TABLE t (a INTEGER, b VARCHAR)")
    altered = _db(tmp_path, "alt", "CREATE TABLE t (a INTEGER, b INTEGER)")
    assert live_schema_fingerprint(base, _SEM) != live_schema_fingerprint(altered, _SEM)
    assert drifted_tables(altered, _SEM) == []  # present, name-wise — not a nameable drop/rename
    base.close()
    altered.close()
