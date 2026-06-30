"""Read-only database access — the dialect seam, the SQL guard, and the connection.

This is the only door to a SQL database in sqbyl. It lives in ``sqbyl-runtime``
because the shipped agent executes its generated SQL through exactly this layer
(spec §5 step 5), so read-only enforcement travels with the runtime.
"""

from __future__ import annotations

from sqbyl_runtime.db.connection import Database, QueryResult, resolve_url
from sqbyl_runtime.db.dialects import (
    DialectAdapter,
    DuckDBAdapter,
    PostgresAdapter,
    PrivilegeReport,
    adapter_for,
)
from sqbyl_runtime.db.errors import (
    StaticValidationError,
    UnparseableSqlError,
    WritablePrivilegeWarning,
    WriteAttemptError,
)
from sqbyl_runtime.db.guard import assert_read_only, is_read_only

__all__ = [
    "Database",
    "DialectAdapter",
    "DuckDBAdapter",
    "PostgresAdapter",
    "PrivilegeReport",
    "QueryResult",
    "StaticValidationError",
    "UnparseableSqlError",
    "WritablePrivilegeWarning",
    "WriteAttemptError",
    "adapter_for",
    "assert_read_only",
    "is_read_only",
    "resolve_url",
]
