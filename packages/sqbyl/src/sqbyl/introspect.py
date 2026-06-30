"""Schema introspector (spec §3 #1, plan 1.2).

Deterministic, $0, no LLM: read tables, columns, types, PK/FK, and any existing DB
comments through SQLAlchemy, and draft one ``TableSemantics`` per table — column
*meaning* is left blank (the annotator fills descriptions/synonyms in Phase 2,
grounded in the profile). FK constraints become high-confidence joins; for
databases without FKs we emit name/type-matched join *candidates* as low-confidence
stubs for a human to confirm (spec §1.2).

This is a dev-authoring tool, so it lives in ``sqbyl`` and reads through the
runtime's read-only ``Database``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

import sqlalchemy as sa

from sqbyl_runtime.db import Database
from sqbyl_runtime.models import Column, Dialect, Join, TableSemantics

# System schemas that are never project tables.
_SYSTEM_SCHEMAS = ("information_schema", "pg_catalog")

# Confidence for a join we inferred from column-name/type matching rather than a
# declared FK. Deliberately low: it is a candidate a human confirms, not a fact.
_HEURISTIC_JOIN_CONFIDENCE = 0.4


@dataclass(frozen=True)
class _RawTable:
    """What introspection reads about one table before we shape joins across tables."""

    schema: str
    name: str
    columns: list[Column]
    comment: str | None
    pk_columns: tuple[str, ...]
    fk_columns: frozenset[str]
    joins: list[Join] = field(default_factory=list)

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"

    def column_type(self, column: str) -> str | None:
        return next((c.type for c in self.columns if c.name == column), None)


def discover_tables(db: Database) -> list[tuple[str, str]]:
    """List ``(schema, table)`` pairs for user tables/views in the current catalog."""
    result = db.execute(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_type IN ('BASE TABLE', 'VIEW') "
        f"AND table_schema NOT IN ({', '.join(repr(s) for s in _SYSTEM_SCHEMAS)}) "
        "ORDER BY table_schema, table_name"
    )
    return [(str(schema), str(name)) for schema, name in result.rows]


def _primary_key_columns(
    db: Database, insp: sa.Inspector, schema: str, table: str
) -> tuple[str, ...]:
    """PK columns, with a DuckDB-native fallback (its SA inspector omits PKs)."""
    pk = insp.get_pk_constraint(table, schema=schema)
    cols = tuple(pk.get("constrained_columns") or ())
    if cols or db.dialect is not Dialect.duckdb:
        return cols
    rows = db.execute(
        "SELECT constraint_column_names FROM duckdb_constraints() "
        "WHERE schema_name = :schema AND table_name = :table "
        "AND constraint_type = 'PRIMARY KEY'",
        params={"schema": schema, "table": table},
    ).rows
    return tuple(cast("list[str]", rows[0][0])) if rows else ()


def _read_table(db: Database, insp: sa.Inspector, schema: str, table: str) -> _RawTable:
    columns = [
        Column(
            name=str(col["name"]),
            # Lowercase the rendered type so drafts read like the hand-authored files.
            type=str(col["type"]).lower(),
            # Carry a DB comment through as the description if one exists; otherwise
            # leave meaning blank for the annotator.
            description=(col.get("comment") or None),
        )
        for col in insp.get_columns(table, schema=schema)
    ]
    try:
        table_comment = insp.get_table_comment(table, schema=schema).get("text")
    except (NotImplementedError, sa.exc.SQLAlchemyError):
        table_comment = None

    fks = insp.get_foreign_keys(table, schema=schema)
    fk_columns: set[str] = set()
    joins: list[Join] = []
    for fk in fks:
        constrained = list(fk["constrained_columns"])
        referred_schema = fk.get("referred_schema") or schema
        referred_table = str(fk["referred_table"])
        referred = list(fk["referred_columns"])
        fk_columns.update(constrained)
        on = " AND ".join(
            f"{table}.{lc} = {referred_table}.{rc}"
            for lc, rc in zip(constrained, referred, strict=True)
        )
        joins.append(
            Join(
                to=f"{referred_schema}.{referred_table}",
                type="many_to_one",
                on=on,
                confidence=1.0,
            )
        )

    return _RawTable(
        schema=schema,
        name=table,
        columns=columns,
        comment=table_comment,
        pk_columns=_primary_key_columns(db, insp, schema, table),
        fk_columns=frozenset(fk_columns),
        joins=joins,
    )


def _add_heuristic_joins(tables: list[_RawTable]) -> None:
    """For columns not already covered by a FK, propose joins to tables whose single
    primary key has the same column name and type. Low confidence, for human review."""
    # Index single-column PKs by (column-name, type) so a foreign-key-shaped column
    # can find the table it most likely references.
    pk_index: dict[tuple[str, str], _RawTable] = {}
    for t in tables:
        if len(t.pk_columns) == 1:
            pk_col = t.pk_columns[0]
            pk_type = t.column_type(pk_col)
            if pk_type is not None:
                pk_index[(pk_col, pk_type)] = t

    for t in tables:
        existing_targets = {j.to for j in t.joins}
        for col in t.columns:
            if col.name in t.fk_columns or col.name in t.pk_columns:
                continue
            target = pk_index.get((col.name, col.type))
            if target is None or target.qualified == t.qualified:
                continue
            if target.qualified in existing_targets:
                continue
            t.joins.append(
                Join(
                    to=target.qualified,
                    type="many_to_one",
                    on=f"{t.name}.{col.name} = {target.name}.{col.name}",
                    confidence=_HEURISTIC_JOIN_CONFIDENCE,
                )
            )
            existing_targets.add(target.qualified)


def introspect(db: Database) -> list[TableSemantics]:
    """Draft a ``TableSemantics`` for every user table in the connected database."""
    insp = sa.inspect(db.engine)
    raw = [_read_table(db, insp, schema, table) for schema, table in discover_tables(db)]
    _add_heuristic_joins(raw)
    return [
        TableSemantics(
            table=t.qualified,
            description=t.comment,
            columns=t.columns,
            joins=t.joins,
        )
        for t in raw
    ]
