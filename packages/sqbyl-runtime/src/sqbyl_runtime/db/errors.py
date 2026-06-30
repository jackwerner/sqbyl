"""Errors and warnings raised by the DB layer (spec §1, §13)."""

from __future__ import annotations


class WriteAttemptError(Exception):
    """Raised when SQL that is not a pure read is submitted under read-only mode.

    The agent, the Coach, the profiler — nothing is allowed to issue DDL/DML when
    the connection is read-only (the default). This is the SQL-layer half of the
    read-only guarantee; the driver/session half (see ``privileges``) is the other.
    """


class UnparseableSqlError(Exception):
    """Raised when the read-only guard cannot parse a statement to prove it is a read.

    A statement we cannot understand is treated as unsafe: the guard fails closed.
    """


class WritablePrivilegeWarning(UserWarning):
    """Emitted on connect when the credential can write but read-only is requested.

    sqbyl refuses non-SELECT at the SQL layer, but that is best-effort; real
    isolation is a read-only DB role. This warning carries a suggested fix (spec §13).
    """
