"""Live-Postgres integration for the read-only DB layer.

The seeded DuckDB fixture exercises the *logic*, but the Postgres adapter's session
read-only enforcement and privilege introspection only really run against a live server.
CI provides one via a service container; these tests **skip** unless
``SQBYL_TEST_POSTGRES_URL`` points at a reachable Postgres. Locally:

    docker run -d --name sqbyl-pg -e POSTGRES_PASSWORD=sqbyl -e POSTGRES_DB=sqbyl_test \\
        -p 55432:5432 postgres:16
    SQBYL_TEST_POSTGRES_URL=postgresql+psycopg://postgres:sqbyl@127.0.0.1:55432/sqbyl_test \\
        uv run --with 'psycopg[binary]' pytest tests/test_postgres_integration.py

$0 — no LLM. Every assertion here is about the DB safety layer (invariant 6).
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from sqbyl_runtime.db import (
    Database,
    WritablePrivilegeWarning,
    WriteAttemptError,
    adapter_for,
)
from sqbyl_runtime.models import Dialect

_PG_URL = os.environ.get("SQBYL_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not _PG_URL,
    reason="set SQBYL_TEST_POSTGRES_URL to run the live-Postgres integration tests",
)


@pytest.fixture(scope="module")
def seeded() -> Iterator[str]:
    """Seed a tiny analytics schema and a SELECT-only role; yield the superuser URL.

    Setup writes go through a raw engine — ``Database`` refuses non-SELECT, which is
    exactly what we test below.
    """
    assert _PG_URL is not None
    engine = sa.create_engine(_PG_URL)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP SCHEMA IF EXISTS analytics CASCADE"))
        conn.execute(sa.text("CREATE SCHEMA analytics"))
        conn.execute(
            sa.text(
                "CREATE TABLE analytics.orders ("
                "order_id int PRIMARY KEY, customer_id int, amount_cents int, status text)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO analytics.orders VALUES "
                "(1, 10, 500, 'confirmed'), (2, 10, 700, 'refunded'), (3, 11, 900, 'confirmed')"
            )
        )
        # A dedicated SELECT-only role — the posture sqbyl recommends.
        conn.execute(sa.text("DROP ROLE IF EXISTS sqbyl_ro"))
        conn.execute(sa.text("CREATE ROLE sqbyl_ro LOGIN PASSWORD 'ro'"))
        conn.execute(sa.text("GRANT USAGE ON SCHEMA analytics TO sqbyl_ro"))
        conn.execute(sa.text("GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO sqbyl_ro"))
    engine.dispose()
    yield _PG_URL


def _readonly_url(superuser_url: str) -> str:
    """Same target, but authenticating as the SELECT-only role.

    ``str(URL)`` masks the password as ``***``; render it out explicitly.
    """
    ro = sa.make_url(superuser_url).set(username="sqbyl_ro", password="ro")
    return ro.render_as_string(hide_password=False)


def test_select_executes_against_live_postgres(seeded: str) -> None:
    with Database.connect(seeded, dialect=Dialect.postgresql, warn=False) as db:
        result = db.execute("SELECT count(*) AS n FROM analytics.orders")
    assert result.rows[0][0] == 3


def test_non_select_refused_by_the_guard(seeded: str) -> None:
    with (
        Database.connect(seeded, dialect=Dialect.postgresql, warn=False) as db,
        pytest.raises(WriteAttemptError),
    ):
        db.execute("INSERT INTO analytics.orders VALUES (99, 1, 1, 'x')")
    # And the row was never written.
    with Database.connect(seeded, dialect=Dialect.postgresql, warn=False) as db:
        assert db.execute("SELECT count(*) FROM analytics.orders").rows[0][0] == 3


def test_session_is_read_only_at_the_server(seeded: str) -> None:
    # Behind the guard: a write issued directly on the read-only engine must be rejected
    # by Postgres itself ("cannot execute INSERT in a read-only transaction"), proving the
    # session-level SET CHARACTERISTICS listener actually took effect on a live server.
    engine = adapter_for(Dialect.postgresql).make_engine(seeded, read_only=True)
    try:
        with engine.connect() as conn, pytest.raises(sa.exc.DBAPIError) as exc:
            conn.execute(sa.text("INSERT INTO analytics.orders VALUES (98, 1, 1, 'x')"))
    finally:
        engine.dispose()
    assert "read-only" in str(exc.value).lower()


def test_writable_superuser_credential_warns(seeded: str) -> None:
    # The default `postgres` credential is a superuser — it can write regardless of the
    # session setting, so connecting must surface the privilege warning with a fix.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        Database.connect(seeded, dialect=Dialect.postgresql).close()
    matches = [w for w in caught if issubclass(w.category, WritablePrivilegeWarning)]
    assert len(matches) == 1
    assert "read-only role" in str(matches[0].message)


def test_select_only_role_does_not_warn(seeded: str) -> None:
    # The recommended posture: a role with only SELECT grants raises no privilege warning.
    with warnings.catch_warnings():
        warnings.simplefilter("error", WritablePrivilegeWarning)
        Database.connect(_readonly_url(seeded), dialect=Dialect.postgresql).close()


def test_introspect_reads_the_postgres_schema(seeded: str) -> None:
    # The dev-side introspector must work against Postgres, not just DuckDB.
    from sqbyl.introspect import introspect

    with Database.connect(seeded, dialect=Dialect.postgresql, warn=False) as db:
        tables = introspect(db)
    orders = next(t for t in tables if t.table == "analytics.orders")
    assert {c.name for c in orders.columns} >= {"order_id", "customer_id", "amount_cents", "status"}
