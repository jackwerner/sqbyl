"""Phase 9.5 — the breadth dialects behind the connection seam (spec §1.1, §13).

SQLite is exercised end-to-end (stdlib, no driver). The warehouse adapters
(Snowflake/BigQuery/MySQL) can't reach a live server in CI, so they're tested at the
seam: read-only posture, privilege reporting, the ``EXPLAIN`` hook, and the friendly
missing-driver hint. Read-only enforcement itself (the SQL guard) is dialect-agnostic
and covered in ``test_db_readonly.py``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import Connection

from sqbyl_runtime.db import (
    BigQueryAdapter,
    Database,
    MySQLAdapter,
    SnowflakeAdapter,
    SQLiteAdapter,
    WritablePrivilegeWarning,
    WriteAttemptError,
    resolve_url,
)
from sqbyl_runtime.models import Dialect


@pytest.fixture
def sqlite_path(tmp_path: Path) -> Path:
    path = tmp_path / "shop.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    con.executemany("INSERT INTO items (name) VALUES (?)", [("a",), ("b",), ("c",)])
    con.commit()
    con.close()
    return path


# --- SQLite: fully exercised -----------------------------------------------------


def test_sqlite_reads_rows(sqlite_path: Path) -> None:
    with Database.connect(str(sqlite_path), dialect=Dialect.sqlite) as db:
        result = db.execute("SELECT count(*) AS n FROM items")
    assert result.rows == [(3,)]


def test_sqlite_read_only_refuses_writes(sqlite_path: Path) -> None:
    # Refused at the SQL guard before it ever reaches the driver...
    with (
        Database.connect(str(sqlite_path), dialect=Dialect.sqlite) as db,
        pytest.raises(WriteAttemptError),
    ):
        db.execute("INSERT INTO items (name) VALUES ('x')")
    # ...and the driver-level query_only pragma is the backstop: even bypassing the guard
    # (raw engine) a write fails, proving read-only isn't guard-only.
    with Database.connect(str(sqlite_path), dialect=Dialect.sqlite) as db:
        from sqlalchemy import text

        with (
            pytest.raises(Exception, match="(?i)readonly|read-only|query_only"),  # noqa: PT011
            db.engine.connect() as conn,
        ):
            conn.execute(text("INSERT INTO items (name) VALUES ('x')"))


def test_sqlite_explain_validates(sqlite_path: Path) -> None:
    # A bad column is caught by EXPLAIN (static validation) without executing.
    with Database.connect(str(sqlite_path), dialect=Dialect.sqlite) as db:
        from sqbyl_runtime.db import StaticValidationError

        with pytest.raises(StaticValidationError):
            db.explain("SELECT no_such_col FROM items")
        db.explain("SELECT name FROM items")  # valid: no raise


def test_sqlite_read_only_does_not_warn(sqlite_path: Path) -> None:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error", WritablePrivilegeWarning)
        Database.connect(str(sqlite_path), dialect=Dialect.sqlite, read_only=True).close()


def test_sqlite_writable_connect_warns(sqlite_path: Path) -> None:
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        Database.connect(str(sqlite_path), dialect=Dialect.sqlite, read_only=False).close()
    matches = [w for w in caught if issubclass(w.category, WritablePrivilegeWarning)]
    assert len(matches) == 1
    assert "Suggested fix" in str(matches[0].message)


def test_sqlite_privilege_report() -> None:
    adapter = SQLiteAdapter()
    ro = adapter.inspect_privileges(_ignored_conn(), read_only=True)
    assert ro.can_write is False and ro.enforced_read_only is True
    rw = adapter.inspect_privileges(_ignored_conn(), read_only=False)
    assert rw.can_write is True and rw.suggested_fix is not None


def test_resolve_url_normalizes_bare_sqlite_path() -> None:
    assert resolve_url("data/x.sqlite", Dialect.sqlite) == "sqlite:///data/x.sqlite"
    assert resolve_url("sqlite:///x", Dialect.sqlite) == "sqlite:///x"  # explicit passes through


# --- warehouse adapters: seam-level ----------------------------------------------


def test_bigquery_skips_explain() -> None:
    # BigQuery has no SQL EXPLAIN → the adapter returns None so static validation is skipped.
    assert BigQueryAdapter().explain_statement("SELECT 1") is None
    # The others still validate via EXPLAIN.
    assert SnowflakeAdapter().explain_statement("SELECT 1") == "EXPLAIN SELECT 1"
    assert MySQLAdapter().explain_statement("SELECT 1") == "EXPLAIN SELECT 1"


class _GrantsConnection:
    """Feeds canned ``SHOW GRANTS`` rows to MySQLAdapter.inspect_privileges (no live server)."""

    def __init__(self, *grant_lines: str) -> None:
        self._grants = [(line,) for line in grant_lines]

    def execute(self, *_args: object, **_kw: object) -> _GrantsConnection:
        return self

    def fetchall(self) -> list[tuple[str]]:
        return self._grants


def test_mysql_privilege_detection() -> None:
    # No MySQL server in CI: drive the SHOW GRANTS scan with stubbed rows, the same way the
    # Postgres adapter is tested. This is the half of read-only safety with no driver hard stop.
    adapter = MySQLAdapter()

    write = adapter.inspect_privileges(
        _GrantsConnection("GRANT SELECT, INSERT, UPDATE ON shop.* TO 'app'@'%'"),  # type: ignore[arg-type]
        read_only=True,
    )
    assert write.can_write is True
    assert write.suggested_fix and "SELECT" in write.suggested_fix

    admin = adapter.inspect_privileges(
        _GrantsConnection("GRANT ALL PRIVILEGES ON *.* TO 'root'@'localhost'"),  # type: ignore[arg-type]
        read_only=True,
    )
    assert admin.can_write is True

    select_only = adapter.inspect_privileges(
        _GrantsConnection("GRANT SELECT ON shop.* TO 'ro'@'%'"),  # type: ignore[arg-type]
        read_only=True,
    )
    assert select_only.can_write is False
    assert select_only.suggested_fix is None
    # Session read-only is still claimed as the driver backstop.
    assert select_only.enforced_read_only is True


def test_warehouse_adapters_take_the_conservative_privilege_posture() -> None:
    # Can't verify a warehouse credential's grants here, so assume writable and warn — the
    # fail-safe posture that steers the user to a least-privilege role.
    for adapter in (SnowflakeAdapter(), BigQueryAdapter()):
        report = adapter.inspect_privileges(_ignored_conn(), read_only=True)
        assert report.can_write is True
        assert report.enforced_read_only is False
        assert report.suggested_fix is not None


@pytest.mark.parametrize(
    ("adapter", "extra"),
    [(SnowflakeAdapter(), "snowflake"), (BigQueryAdapter(), "bigquery")],
)
def test_missing_warehouse_driver_gives_install_hint(adapter: object, extra: str) -> None:
    # These dialects are external SQLAlchemy plugins; when the driver isn't installed,
    # make_engine translates the plugin error into an actionable pip hint.
    plugin = {"snowflake": "snowflake.sqlalchemy", "bigquery": "sqlalchemy_bigquery"}[extra]
    try:
        __import__(plugin)
        pytest.skip(f"{plugin} is installed; the missing-driver path can't be exercised")
    except ImportError:
        pass
    with pytest.raises(ModuleNotFoundError, match=extra):
        adapter.make_engine(  # type: ignore[attr-defined]
            f"{extra}://user:pass@host/db", read_only=True
        )


def _ignored_conn() -> Connection:
    """A stand-in for adapters whose privilege report ignores the connection."""
    return None  # type: ignore[return-value]
