"""The dialect seam — thin, but real (spec §1.1, §13).

DuckDB + Postgres are the M0 dialects; SQLite is the lightest test dialect;
Snowflake / BigQuery / MySQL are the breadth dialects (Phase 9). Each adapter knows
the dialect-specific things about *connecting*: how to build the engine, how to
enforce read-only at the driver/session level, how to inspect the credential's write
privileges, and how (or whether) to statically validate via ``EXPLAIN``. Query
*generation* (profiling SQL, introspection) is layered on top in the dev toolkit; this
seam stays about connection concerns so the runtime stays minimal.

**Driver footprint.** DuckDB is a hard dependency (the default). Postgres, MySQL,
Snowflake, and BigQuery drivers are *optional extras* so the minimal runtime stays
light — ``make_engine`` translates a missing-driver error into a clear ``pip install
sqbyl-runtime[<dialect>]`` hint. SQLite needs no driver (Python stdlib).

**Tested surface.** DuckDB, Postgres, and SQLite are exercised in CI; the warehouse
adapters (Snowflake/BigQuery/MySQL) are written against each dialect's documented
behavior but can't be run in CI without a live warehouse, so they take the honest
conservative posture where they can't verify (see each class).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from sqlalchemy import Connection, event, text
from sqlalchemy.engine import Engine, create_engine
from sqlalchemy.exc import NoSuchModuleError

from sqbyl_runtime.models import Dialect


def _engine_or_driver_hint(url: str, *, extra: str, **kwargs: object) -> Engine:
    """Build an engine, turning a missing dialect driver into an actionable message.

    The optional dialect drivers aren't installed by default (keeps the runtime light);
    when one is missing, SQLAlchemy raises ``NoSuchModuleError``. Translate that into a
    ``pip install`` hint instead of leaking an opaque plugin error.
    """
    try:
        return create_engine(url, **kwargs)
    except NoSuchModuleError as exc:
        raise ModuleNotFoundError(
            f"the {extra!r} dialect driver isn't installed; "
            f"run `pip install 'sqbyl-runtime[{extra}]'` (or the same extra on `sqbyl`)"
        ) from exc


@dataclass(frozen=True)
class PrivilegeReport:
    """What the connected credential can do, and whether sqbyl can hard-stop writes.

    ``can_write`` is the credential's ability *absent* sqbyl's SQL guard — it drives
    the on-connect warning. ``enforced_read_only`` records whether there is a
    driver/session hard stop underneath the guard (defense in depth).
    """

    can_write: bool
    enforced_read_only: bool
    detail: str
    suggested_fix: str | None = None


class DialectAdapter(ABC):
    """Per-dialect connection behavior."""

    dialect: Dialect

    @abstractmethod
    def make_engine(self, url: str, *, read_only: bool) -> Engine:
        """Build a SQLAlchemy engine, enforcing read-only at the driver/session level
        where the dialect supports it."""

    @abstractmethod
    def inspect_privileges(self, conn: Connection, *, read_only: bool) -> PrivilegeReport:
        """Inspect the credential's write privileges over a live connection."""

    def explain_statement(self, sql: str) -> str | None:
        """The statement that statically validates ``sql`` without running it, or ``None``
        to skip static validation on a dialect with no SQL-level ``EXPLAIN`` (e.g. BigQuery,
        whose dry-run is an API concern). Default: standard ``EXPLAIN <query>``."""
        return f"EXPLAIN {sql}"


class DuckDBAdapter(DialectAdapter):
    """DuckDB: file-backed, single-process. Read-only is enforced by the driver
    (``read_only=True``), which is genuine isolation — DuckDB refuses any write."""

    dialect = Dialect.duckdb

    def make_engine(self, url: str, *, read_only: bool) -> Engine:
        # duckdb-engine honors connect_args['read_only'] for file databases.
        return create_engine(url, connect_args={"read_only": read_only} if read_only else {})

    def inspect_privileges(self, conn: Connection, *, read_only: bool) -> PrivilegeReport:
        if read_only:
            return PrivilegeReport(
                can_write=False,
                enforced_read_only=True,
                detail="database opened read-only at the DuckDB driver level",
            )
        return PrivilegeReport(
            can_write=True,
            enforced_read_only=False,
            detail="database opened writable (read_only=false)",
            suggested_fix=(
                "set `read_only: true` in sqbyl.yaml so sqbyl opens the DuckDB file "
                "read-only at the driver level"
            ),
        )


class PostgresAdapter(DialectAdapter):
    """Postgres: enforce read-only per session (``SET SESSION CHARACTERISTICS AS
    TRANSACTION READ ONLY``) and additionally warn if the credential holds write
    grants — best-effort isolation is no substitute for a dedicated read-only role."""

    dialect = Dialect.postgresql

    def make_engine(self, url: str, *, read_only: bool) -> Engine:
        engine = create_engine(url)
        if read_only:

            @event.listens_for(engine, "connect")
            def _set_session_read_only(dbapi_conn: object, _record: object) -> None:
                cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
                try:
                    cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
                finally:
                    cur.close()

        return engine

    def inspect_privileges(self, conn: Connection, *, read_only: bool) -> PrivilegeReport:
        # Superusers bypass read-only grants checks; otherwise look for any write grant.
        is_super = bool(
            conn.execute(text("SELECT usesuper FROM pg_user WHERE usename = current_user")).scalar()
        )
        write_grants = int(
            conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.role_table_grants "
                    "WHERE grantee = current_user "
                    "AND privilege_type IN ('INSERT','UPDATE','DELETE','TRUNCATE')"
                )
            ).scalar()
            or 0
        )
        can_write = is_super or write_grants > 0
        fix = None
        if can_write:
            fix = (
                "point DATABASE_URL at a dedicated read-only role "
                "(GRANT SELECT only) instead of a role that can write"
            )
        return PrivilegeReport(
            can_write=can_write,
            enforced_read_only=read_only,  # session set to READ ONLY above
            detail=(
                "superuser credential"
                if is_super
                else f"credential holds {write_grants} write grant(s)"
            ),
            suggested_fix=fix,
        )


class SQLiteAdapter(DialectAdapter):
    """SQLite: file-backed or in-memory, single-process, no roles. Read-only is enforced
    per connection with ``PRAGMA query_only = ON`` — genuine (SQLite refuses any write
    while it's set), the same defense-in-depth posture as Postgres' session read-only."""

    dialect = Dialect.sqlite

    def make_engine(self, url: str, *, read_only: bool) -> Engine:
        engine = create_engine(url)
        if read_only:

            @event.listens_for(engine, "connect")
            def _set_query_only(dbapi_conn: object, _record: object) -> None:
                cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
                try:
                    cur.execute("PRAGMA query_only = ON")
                finally:
                    cur.close()

        return engine

    def inspect_privileges(self, conn: Connection, *, read_only: bool) -> PrivilegeReport:
        # SQLite has no grant system; writability is the file/handle mode. With
        # query_only ON we hard-stop writes for the session regardless.
        if read_only:
            return PrivilegeReport(
                can_write=False,
                enforced_read_only=True,
                detail="connection set query_only=ON (SQLite refuses writes)",
            )
        return PrivilegeReport(
            can_write=True,
            enforced_read_only=False,
            detail="database opened writable (read_only=false)",
            suggested_fix=(
                "set `read_only: true` in sqbyl.yaml so sqbyl sets query_only=ON, or open "
                "the file with `?mode=ro` in the URL"
            ),
        )


class MySQLAdapter(DialectAdapter):
    """MySQL: enforce read-only per session (``SET SESSION TRANSACTION READ ONLY``) and
    warn if the credential holds write grants. Driver is an optional extra.

    MySQL *can* introspect its own grants (``SHOW GRANTS`` always works), so — like
    Postgres, and unlike the warehouse adapters that can't — it *determines* writability
    from them rather than assuming the worst. The ``SHOW GRANTS`` write-token scan is
    covered by a seam-level test; the ``SET SESSION TRANSACTION READ ONLY`` listener and a
    real ``SHOW GRANTS`` round-trip are **not exercised against a live server in CI** (no
    MySQL in CI) — they're written to MySQL's documented behavior."""

    dialect = Dialect.mysql

    def make_engine(self, url: str, *, read_only: bool) -> Engine:
        engine = _engine_or_driver_hint(url, extra="mysql")
        if read_only:

            @event.listens_for(engine, "connect")
            def _set_session_read_only(dbapi_conn: object, _record: object) -> None:
                cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
                try:
                    cur.execute("SET SESSION TRANSACTION READ ONLY")
                finally:
                    cur.close()

        return engine

    def inspect_privileges(self, conn: Connection, *, read_only: bool) -> PrivilegeReport:
        # SHOW GRANTS lists the current user's privileges; treat any write/admin grant as
        # writable. Best-effort textual scan — MySQL has no tidy privilege table like PG.
        grants = [str(row[0]).upper() for row in conn.execute(text("SHOW GRANTS")).fetchall()]
        write_tokens = ("ALL PRIVILEGES", "INSERT", "UPDATE", "DELETE", "SUPER", "CREATE", "DROP")
        can_write = any(tok in g for g in grants for tok in write_tokens)
        fix = None
        if can_write:
            fix = (
                "point the connection at a user granted only SELECT "
                "(GRANT SELECT ... instead of a write/admin grant)"
            )
        return PrivilegeReport(
            can_write=can_write,
            enforced_read_only=read_only,  # session set READ ONLY above
            detail="credential holds a write/admin grant" if can_write else "SELECT-only grants",
            suggested_fix=fix,
        )


class _WarehouseAdapter(DialectAdapter):
    """Shared posture for cloud warehouses (Snowflake, BigQuery) sqbyl can't verify in CI.

    These dialects have no simple per-session read-only toggle, and their privilege model
    is IAM/role-based rather than a queryable grants table. So the honest posture is: build
    the engine, do **not** claim a driver/session hard stop, and — since we can't prove the
    credential is SELECT-only — report ``can_write=True`` so the on-connect warning fires,
    steering the user to a least-privilege role. Read-only then rests on sqbyl's SQL guard
    (always on) plus that role. Subclasses set ``dialect`` / ``_extra`` / ``_role_fix``."""

    _extra: str
    _role_fix: str

    def make_engine(self, url: str, *, read_only: bool) -> Engine:
        # No session read-only pragma exists; read-only is enforced by the SQL guard and a
        # least-privilege role, not the driver.
        return _engine_or_driver_hint(url, extra=self._extra)

    def inspect_privileges(self, conn: Connection, *, read_only: bool) -> PrivilegeReport:
        return PrivilegeReport(
            can_write=True,  # unverifiable → assume writable and warn (fail safe)
            enforced_read_only=False,
            detail=(
                f"{self.dialect.value} privileges are role/IAM-based and not verified here; "
                "assuming the credential can write"
            ),
            suggested_fix=self._role_fix,
        )


class SnowflakeAdapter(_WarehouseAdapter):
    """Snowflake: engine via the optional ``snowflake-sqlalchemy`` driver; read-only rests
    on the SQL guard + a SELECT-only role. Supports ``EXPLAIN`` for static validation."""

    dialect = Dialect.snowflake
    _extra = "snowflake"
    _role_fix = "use a Snowflake role granted only SELECT/USAGE on the target objects"


class BigQueryAdapter(_WarehouseAdapter):
    """BigQuery: engine via the optional ``sqlalchemy-bigquery`` driver; read-only rests on
    the SQL guard + a read-only IAM role. BigQuery has no SQL ``EXPLAIN`` (planning is a
    dry-run API call), so static validation is skipped — execution is still guarded."""

    dialect = Dialect.bigquery
    _extra = "bigquery"
    _role_fix = "use a service account with a read-only BigQuery role (e.g. dataViewer/jobUser)"

    def explain_statement(self, sql: str) -> str | None:
        return None  # no SQL-level EXPLAIN; skip static validation (execution stays guarded)


_ADAPTERS: dict[Dialect, DialectAdapter] = {
    Dialect.duckdb: DuckDBAdapter(),
    Dialect.postgresql: PostgresAdapter(),
    Dialect.sqlite: SQLiteAdapter(),
    Dialect.mysql: MySQLAdapter(),
    Dialect.snowflake: SnowflakeAdapter(),
    Dialect.bigquery: BigQueryAdapter(),
}


def adapter_for(dialect: Dialect) -> DialectAdapter:
    """Resolve the connection adapter for a dialect."""
    try:
        return _ADAPTERS[dialect]
    except KeyError:  # pragma: no cover - every Dialect member is registered above
        raise NotImplementedError(f"no adapter registered for dialect {dialect.value!r}") from None
