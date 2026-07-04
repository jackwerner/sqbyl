"""Phase 1.1 — read-only DB connection + SQL guard + privilege warning (spec §1, §13).

Exit criteria: connecting with a writable role emits the warning; a non-SELECT is
refused; all tested against the seeded DuckDB fixture with zero external deps.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from sqbyl_runtime.db import (
    Database,
    UnparseableSqlError,
    WritablePrivilegeWarning,
    WriteAttemptError,
    adapter_for,
    is_read_only,
    resolve_url,
)
from sqbyl_runtime.models import Dialect


def _connect(path: Path, **kw: object) -> Database:
    return Database.connect(str(path), dialect=Dialect.duckdb, **kw)  # type: ignore[arg-type]


def test_select_executes_and_returns_rows(duckdb_path: Path) -> None:
    with _connect(duckdb_path) as db:
        result = db.execute(
            "SELECT status, count(*) AS n FROM analytics.orders GROUP BY status ORDER BY status"
        )
    assert result.columns[0] == "status"
    assert dict(result.dicts()[0]).keys() == {"status", "n"}
    assert {row[0] for row in result.rows} == {"confirmed", "refunded", "partial_refund"}


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE analytics.x (i INT)",
        "DROP TABLE analytics.orders",
        "UPDATE analytics.orders SET status = 'x'",
        "DELETE FROM analytics.orders",
        "INSERT INTO analytics.orders VALUES (1, 1, 1, 'x', now())",
        "COPY analytics.orders TO 'leak.csv'",
        "ATTACH 'other.db'",
        "SELECT 1; SELECT 2",  # multiple statements
        # a data-modifying CTE whose root still parses as SELECT
        "WITH d AS (DELETE FROM analytics.orders RETURNING *) SELECT * FROM d",
    ],
)
def test_non_select_is_refused(duckdb_path: Path, sql: str) -> None:
    with _connect(duckdb_path) as db, pytest.raises(WriteAttemptError):
        db.execute(sql)


def test_unparseable_sql_fails_closed(duckdb_path: Path) -> None:
    with _connect(duckdb_path) as db, pytest.raises(UnparseableSqlError):
        db.execute("SELECT FROM WHERE )(")


def test_is_read_only_classifies_statements() -> None:
    assert is_read_only("SELECT 1", dialect=Dialect.duckdb)
    assert is_read_only("WITH x AS (SELECT 1) SELECT * FROM x", dialect=Dialect.duckdb)
    assert is_read_only("SELECT 1 UNION SELECT 2", dialect=Dialect.duckdb)
    assert not is_read_only("DELETE FROM t", dialect=Dialect.duckdb)
    assert not is_read_only("PRAGMA show_tables", dialect=Dialect.duckdb)


def test_read_only_connect_does_not_warn(duckdb_path: Path) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", WritablePrivilegeWarning)
        # read-only DuckDB is driver-enforced; the credential genuinely cannot write.
        _connect(duckdb_path, read_only=True).close()


def test_writable_connect_warns_with_suggested_fix(duckdb_path: Path) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _connect(duckdb_path, read_only=False).close()
    matches = [w for w in caught if issubclass(w.category, WritablePrivilegeWarning)]
    assert len(matches) == 1
    assert "Suggested fix" in str(matches[0].message)


def test_env_indirection(monkeypatch: pytest.MonkeyPatch, duckdb_path: Path) -> None:
    monkeypatch.setenv("SQBYL_TEST_DB", str(duckdb_path))
    url = resolve_url("env:SQBYL_TEST_DB", Dialect.duckdb)
    assert url == f"duckdb:///{duckdb_path}"
    monkeypatch.delenv("SQBYL_TEST_DB", raising=False)
    with pytest.raises(ValueError, match="SQBYL_TEST_DB"):
        resolve_url("env:SQBYL_TEST_DB", Dialect.duckdb)


def test_resolve_url_normalizes_bare_path() -> None:
    # A relative bare path becomes a 3-slash DuckDB URL; an absolute path keeps its
    # leading slash (the SQLAlchemy 4-slash form).
    assert resolve_url("data/x.duckdb", Dialect.duckdb) == "duckdb:///data/x.duckdb"
    assert resolve_url("/data/x.duckdb", Dialect.duckdb) == "duckdb:////data/x.duckdb"
    # An explicit SQLAlchemy URL is passed through untouched.
    assert resolve_url("duckdb:///x", Dialect.duckdb) == "duckdb:///x"


def test_every_dialect_has_an_adapter() -> None:
    # Phase 9.5 registered the breadth dialects; every Dialect member now resolves.
    for dialect in Dialect:
        assert adapter_for(dialect).dialect is dialect


class _StubResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar(self) -> object:
        return self._value


class _StubConnection:
    """Feeds canned scalars to the Postgres privilege queries in order."""

    def __init__(self, *scalars: object) -> None:
        self._scalars = list(scalars)

    def execute(self, *_args: object, **_kw: object) -> _StubResult:
        return _StubResult(self._scalars.pop(0))


def test_postgres_privilege_detection() -> None:
    # No Postgres server in CI: drive inspect_privileges with stubbed rows. This is
    # the half of read-only safety that can't lean on a driver hard-stop (spec §13).
    from sqbyl_runtime.db import PostgresAdapter

    adapter = PostgresAdapter()

    # Superuser → can write, with a read-only-role suggested fix.
    super_report = adapter.inspect_privileges(
        _StubConnection(True, 0),  # type: ignore[arg-type]
        read_only=True,
    )
    assert super_report.can_write is True
    assert super_report.suggested_fix and "read-only role" in super_report.suggested_fix

    # Non-super with write grants → can write.
    grant_report = adapter.inspect_privileges(
        _StubConnection(False, 3),  # type: ignore[arg-type]
        read_only=True,
    )
    assert grant_report.can_write is True

    # Non-super, no write grants → SELECT-only, no warning needed.
    ro_report = adapter.inspect_privileges(
        _StubConnection(False, 0),  # type: ignore[arg-type]
        read_only=True,
    )
    assert ro_report.can_write is False
    assert ro_report.suggested_fix is None
