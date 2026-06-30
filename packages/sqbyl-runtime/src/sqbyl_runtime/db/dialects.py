"""The dialect seam — thin, but real (spec §1.1, §13).

DuckDB + Postgres are the M0 dialects. Each adapter knows three dialect-specific
things about *connecting*: how to build the engine, how to enforce read-only at the
driver/session level, and how to inspect the credential's write privileges. Query
*generation* (profiling SQL, introspection) is layered on top in the dev toolkit;
this seam stays about connection concerns so the runtime stays minimal.

Snowflake/BigQuery/MySQL slot in here in Phase 9 without touching callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from sqlalchemy import Connection, event, text
from sqlalchemy.engine import Engine, create_engine

from sqbyl_runtime.models import Dialect


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


_ADAPTERS: dict[Dialect, DialectAdapter] = {
    Dialect.duckdb: DuckDBAdapter(),
    Dialect.postgresql: PostgresAdapter(),
}


def adapter_for(dialect: Dialect) -> DialectAdapter:
    """Resolve the adapter for a dialect, or raise for the not-yet-supported ones."""
    try:
        return _ADAPTERS[dialect]
    except KeyError:
        raise NotImplementedError(
            f"dialect {dialect.value!r} is not an M0 dialect; "
            f"DuckDB and Postgres are supported (the rest land in Phase 9)"
        ) from None
