"""The read-only SQL guard (spec §1, §13).

Refuse anything that is not a single, pure read. We parse with sqlglot and apply
two rules, failing *closed*:

1. Exactly one statement (so a write can't be smuggled after a SELECT).
2. The statement is a query (SELECT / set-operation / subquery) and contains no
   write or DDL node *anywhere* in its tree — Postgres allows a data-modifying CTE
   (``WITH d AS (DELETE ... RETURNING *) SELECT * FROM d``) whose root still parses
   as a SELECT, so a root-only check is not enough.

This is the SQL-layer half of the read-only guarantee; the driver/session half
lives in ``privileges``/``connection``. Both are kept on at once (defense in depth).
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from sqbyl_runtime.db.errors import UnparseableSqlError, WriteAttemptError
from sqbyl_runtime.models import Dialect

# Root node types that represent a pure read.
_QUERY_ROOTS: tuple[type[exp.Expression], ...] = (
    exp.Select,
    exp.Union,
    exp.Intersect,
    exp.Except,
    exp.Subquery,
)

# Any of these appearing anywhere in the tree means the statement can mutate state
# (DML, DDL, or a side-effecting command). Built by name so it stays robust across
# sqlglot versions that add/rename expression classes.
_FORBIDDEN_NODE_NAMES = (
    "Insert",
    "Update",
    "Delete",
    "Merge",
    "Create",
    "Drop",
    "Alter",
    "TruncateTable",
    "Copy",
    "Attach",
    "Detach",
    "Grant",
    "Set",
    "SetItem",
    "Use",
    "Pragma",
    "Command",  # sqlglot's catch-all for statements it can't model (e.g. CALL, VACUUM)
)
_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(
    node
    for name in _FORBIDDEN_NODE_NAMES
    if isinstance(node := getattr(exp, name, None), type) and issubclass(node, exp.Expression)
)


def _sqlglot_dialect(dialect: Dialect) -> str:
    """Map a sqbyl dialect to the name sqlglot parses with."""
    # sqbyl uses 'postgresql'; sqlglot's reader is named 'postgres'. The rest match.
    return "postgres" if dialect is Dialect.postgresql else dialect.value


def is_read_only(sql: str, *, dialect: Dialect) -> bool:
    """True iff ``sql`` is a single, pure read. Fails closed on parse errors."""
    try:
        statements = [
            s for s in sqlglot.parse(sql, read=_sqlglot_dialect(dialect)) if s is not None
        ]
    except SqlglotError:
        return False
    if len(statements) != 1:
        return False
    root = statements[0]
    if not isinstance(root, _QUERY_ROOTS):
        return False
    return root.find(*_FORBIDDEN_NODES) is None


def assert_read_only(sql: str, *, dialect: Dialect) -> None:
    """Raise unless ``sql`` is a single, pure read.

    ``UnparseableSqlError`` if we can't parse it (unsafe by default), else
    ``WriteAttemptError`` if it is parseable but not a pure read.
    """
    try:
        sqlglot.parse(sql, read=_sqlglot_dialect(dialect))
    except SqlglotError as exc:
        raise UnparseableSqlError(f"refusing SQL the read-only guard cannot parse: {exc}") from exc
    if not is_read_only(sql, dialect=dialect):
        raise WriteAttemptError(
            "read-only mode refuses non-SELECT SQL "
            "(only a single SELECT / set-operation / subquery is allowed)"
        )
