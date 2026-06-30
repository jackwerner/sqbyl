"""Usage accounting — meter every paid call to ``.sqbyl/usage.db`` (invariant 5).

A small SQLite table is the durable ledger behind the live spend meter and the
``--budget`` cap. Every record links to the model/role that spent and (optionally)
the ``run_id`` and project ``content_hash``, so spend is always attributable to the
exact config that produced it.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from sqbyl_runtime.llm.base import Usage

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    command TEXT,
    role TEXT,
    model TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,
    run_id TEXT,
    content_hash TEXT
);
"""


class UsageRecord(BaseModel):
    """One metered call."""

    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    command: str | None = None
    role: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float | None = None
    run_id: str | None = None
    content_hash: str | None = None

    @classmethod
    def from_usage(
        cls,
        usage: Usage,
        *,
        model: str | None = None,
        command: str | None = None,
        role: str | None = None,
        cost_usd: float | None = None,
        run_id: str | None = None,
        content_hash: str | None = None,
    ) -> UsageRecord:
        """Build a record from an :class:`Usage` returned by the LLM seam."""
        return cls(
            command=command,
            role=role,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens,
            cache_read_input_tokens=usage.cache_read_input_tokens,
            cost_usd=cost_usd,
            run_id=run_id,
            content_hash=content_hash,
        )


class UsageStore:
    """SQLite-backed usage ledger."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def record(self, record: UsageRecord) -> int:
        """Append a usage row; returns its id."""
        cur = self._conn.execute(
            """
            INSERT INTO usage (
                ts, command, role, model,
                input_tokens, output_tokens,
                cache_creation_input_tokens, cache_read_input_tokens,
                cost_usd, run_id, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.ts.isoformat(),
                record.command,
                record.role,
                record.model,
                record.input_tokens,
                record.output_tokens,
                record.cache_creation_input_tokens,
                record.cache_read_input_tokens,
                record.cost_usd,
                record.run_id,
                record.content_hash,
            ),
        )
        self._conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def all(self) -> list[UsageRecord]:
        """Read every usage row back as models, oldest first."""
        rows = self._conn.execute("SELECT * FROM usage ORDER BY id").fetchall()
        return [
            UsageRecord(
                ts=datetime.fromisoformat(row["ts"]),
                command=row["command"],
                role=row["role"],
                model=row["model"],
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                cache_creation_input_tokens=row["cache_creation_input_tokens"],
                cache_read_input_tokens=row["cache_read_input_tokens"],
                cost_usd=row["cost_usd"],
                run_id=row["run_id"],
                content_hash=row["content_hash"],
            )
            for row in rows
        ]

    def total_cost(self, *, run_id: str | None = None) -> float:
        """Sum metered cost, optionally for a single run."""
        if run_id is None:
            row = self._conn.execute("SELECT COALESCE(SUM(cost_usd), 0.0) FROM usage").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM usage WHERE run_id = ?", (run_id,)
            ).fetchone()
        return float(row[0])

    def total_tokens(self) -> int:
        """Sum all token counts across every metered call."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(input_tokens + output_tokens + "
            "cache_creation_input_tokens + cache_read_input_tokens), 0) FROM usage"
        ).fetchone()
        return int(row[0])

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> UsageStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
