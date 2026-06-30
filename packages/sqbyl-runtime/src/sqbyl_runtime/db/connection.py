"""The read-only database connection (spec §1, §13).

``Database`` is the one entry point both the runtime (``ask`` executes here) and the
dev toolkit (introspect/profile read here) use to reach a SQL database. It is
read-only by default and read-only on three independent levels:

1. the SQL guard refuses anything that isn't a single pure read (``db.guard``);
2. the driver/session is put in a read-only mode where the dialect supports it;
3. on connect, if the credential can still write, a ``WritablePrivilegeWarning``
   fires with a suggested fix.

Credentials never appear as literals: the connection URL supports ``env:NAME``
indirection (spec §4).
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass
from types import TracebackType

from sqlalchemy import text
from sqlalchemy.engine import Engine

from sqbyl_runtime.db.dialects import DialectAdapter, PrivilegeReport, adapter_for
from sqbyl_runtime.db.errors import WritablePrivilegeWarning
from sqbyl_runtime.db.guard import assert_read_only
from sqbyl_runtime.models import Dialect


@dataclass(frozen=True)
class QueryResult:
    """Columns + rows from a read. Deliberately plain so it serializes into traces."""

    columns: list[str]
    rows: list[tuple[object, ...]]

    def dicts(self) -> list[dict[str, object]]:
        return [dict(zip(self.columns, row, strict=True)) for row in self.rows]


def resolve_url(raw: str, dialect: Dialect) -> str:
    """Expand ``env:NAME`` indirection and normalize a bare path into a SQLAlchemy URL.

    A bare filesystem path with no scheme is treated as a DuckDB file path so
    ``DATABASE_URL=/path/to.duckdb`` (or a raw path in the manifest) just works.
    """
    url = raw.strip()
    if url.startswith("env:"):
        name = url[len("env:") :]
        value = os.environ.get(name)
        if not value:
            raise ValueError(f"database url references env:{name}, but ${name} is unset or empty")
        url = value.strip()
    if "://" not in url and dialect is Dialect.duckdb:
        url = f"duckdb:///{url}"
    return url


class Database:
    """A live, read-only-by-default connection to one SQL database."""

    def __init__(
        self,
        engine: Engine,
        *,
        dialect: Dialect,
        adapter: DialectAdapter,
        read_only: bool,
        privileges: PrivilegeReport,
    ) -> None:
        self._engine = engine
        self.dialect = dialect
        self._adapter = adapter
        self.read_only = read_only
        self.privileges = privileges

    @classmethod
    def connect(
        cls,
        url: str,
        *,
        dialect: Dialect,
        read_only: bool = True,
        warn: bool = True,
    ) -> Database:
        """Open a connection, run the privilege check, and warn if it can still write."""
        adapter = adapter_for(dialect)
        engine = adapter.make_engine(resolve_url(url, dialect), read_only=read_only)
        with engine.connect() as conn:
            privileges = adapter.inspect_privileges(conn, read_only=read_only)
        if warn and privileges.can_write:
            # The credential can write. In read-only mode the SQL guard is the
            # (best-effort) backstop; with read_only disabled there is no backstop.
            posture = (
                "sqbyl refuses non-SELECT at the SQL layer, but that is best-effort"
                if read_only
                else "read_only is disabled, so the SQL guard is OFF"
            )
            fix = f" Suggested fix: {privileges.suggested_fix}" if privileges.suggested_fix else ""
            warnings.warn(
                f"the database credential can write ({privileges.detail}); {posture}.{fix}",
                WritablePrivilegeWarning,
                stacklevel=2,
            )
        return cls(
            engine,
            dialect=dialect,
            adapter=adapter,
            read_only=read_only,
            privileges=privileges,
        )

    @property
    def engine(self) -> Engine:
        """The underlying SQLAlchemy engine (introspection/profiling read through it)."""
        return self._engine

    def execute(self, sql: str, *, params: dict[str, object] | None = None) -> QueryResult:
        """Run a read and return its rows. Refuses non-SELECT when read-only."""
        if self.read_only:
            assert_read_only(sql, dialect=self.dialect)
        with self._engine.connect() as conn:
            result = conn.execute(text(sql), params or {})
            columns = list(result.keys())
            rows = [tuple(row) for row in result.fetchall()]
        return QueryResult(columns=columns, rows=rows)

    def close(self) -> None:
        self._engine.dispose()

    def __enter__(self) -> Database:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
