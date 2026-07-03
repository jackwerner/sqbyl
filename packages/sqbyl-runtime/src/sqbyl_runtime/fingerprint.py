"""Schema & knowledge fingerprinting (spec §11).

Two stable hashes that keep a shipped agent honest:

* :func:`schema_fingerprint` / :func:`fingerprint_semantics` — a hash of the database
  *schema* (each table's qualified name plus its columns' names and rendered types).
  Stamped onto a release at build time and recomputed against the live DB at
  :func:`sqbyl_runtime.load`, so a renamed, dropped, or altered table (the one thing
  that silently breaks a shipped agent) surfaces as a non-fatal warning.

* :func:`fingerprint_knowledge` — a hash of the agent's whole *brain* (semantics +
  instructions + examples + trusted assets + dialect + selection). Stamped onto an
  eval run and recomputed at release time, so a scorecard's accuracy can be tied to
  the exact project files that earned it — not just the schema, since an edited
  example or measure drifts a score just as silently as a renamed table.

These live in the runtime because both sides must produce the **same** hash: the dev
builder (``sqbyl release create``) computes them from the working files, and the
runtime recomputes them at load / eval time. Keep the normalization here so the two
can never drift.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import TYPE_CHECKING

from sqbyl_runtime.models import TableSemantics

if TYPE_CHECKING:
    from sqbyl_runtime.context import ProjectKnowledge
    from sqbyl_runtime.db import Database

# A normalized schema: an ordered list of (qualified table name, ordered
# (column name, rendered lowercase type)). The shape both sides reduce to before
# hashing, so a fingerprint depends only on structure — never on descriptions,
# synonyms, or file layout.
SchemaShape = Sequence[tuple[str, Sequence[tuple[str, str]]]]


def schema_fingerprint(tables: SchemaShape) -> str:
    """Hash a normalized schema. Tables are sorted (order-independent); columns keep
    their declared order (a reordering is a real schema change on most engines)."""
    digest = hashlib.sha256()
    for table, columns in sorted(tables, key=lambda t: t[0]):
        digest.update(table.encode())
        for name, type_ in columns:
            digest.update(f"\0{name}:{type_}".encode())
        digest.update(b"\0\0")
    return "sha256:" + digest.hexdigest()


def fingerprint_semantics(semantics: Sequence[TableSemantics]) -> str:
    """The fingerprint of a schema as captured by its :class:`TableSemantics` — the shipped
    brain's *declared* view of the schema. Note this reads the YAML ``type`` strings, which a
    human can edit and which need not match a live inspector's rendering (``text`` vs
    ``varchar``); for the build↔load mismatch check use :func:`live_schema_fingerprint` on both
    sides instead, so the same normalization is applied. This one is for schema *content*
    hashing where no live DB is in play."""
    return schema_fingerprint([(t.table, [(c.name, c.type) for c in t.columns]) for t in semantics])


# A live column's type is rendered by SQLAlchemy's ``__str__``, which is *not* contractually
# stable across driver/SQLAlchemy versions (``VARCHAR`` vs ``VARCHAR(255)``; ``TEXT`` vs
# ``VARCHAR``). We normalize coarsely — drop length/precision args and collapse the string
# family — so a driver upgrade or a ``text``↔``varchar`` rendering difference doesn't fire a
# false drift warning. The guarantee is only "same *family*", the right granularity for "did a
# table the agent references change shape", not exact DDL equality.
_MISSING = "<missing>"
_TYPE_FAMILY = {
    "varchar": "string",
    "text": "string",
    "char": "string",
    "bpchar": "string",
    "nvarchar": "string",
    "string": "string",
    "character": "string",
    "character varying": "string",
}


def _normalize_type(rendered: object) -> str:
    """Coarse, version-tolerant type token: lowercased, length/precision stripped, string
    family collapsed. Widths stay distinct (``bigint`` ≠ ``int``); only the string family —
    the demonstrated ``text`` vs ``varchar`` false-positive — is merged."""
    base = str(rendered).split("(")[0].strip().lower()
    return _TYPE_FAMILY.get(base, base)


def _live_columns(db: Database, table: str) -> dict[str, str] | None:
    """Live columns of one qualified ``schema.table`` as ``{lowercased name: normalized type}``,
    or ``None`` if the table doesn't exist. Names are case-folded because identifier
    case-folding differs by dialect (a brain's ``Orders`` vs an inspector's ``orders`` is not a
    real drift)."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.exc import NoSuchTableError

    schema, _, name = table.rpartition(".")
    try:
        cols = sa_inspect(db.engine).get_columns(name, schema=schema or None)
    except NoSuchTableError:
        return None
    return {str(c["name"]).lower(): _normalize_type(c["type"]) for c in cols}


def live_schema_fingerprint(db: Database, semantics: Sequence[TableSemantics]) -> str:
    """The fingerprint of the **live** database, restricted to the columns the brain actually
    *declares* (so an additive migration that adds an unreferenced column doesn't false-fire)
    with types normalized coarsely (:func:`_normalize_type`) so a driver-version rendering
    difference doesn't either.

    This is the one comparison that must be apples-to-apples: computed here at eval time
    (stamped onto the run, then the release) and recomputed identically at
    :func:`sqbyl_runtime.load` against the injected DB. A **healthy DB within the same driver
    stack** fingerprints the same on both sides, while a dropped/renamed declared table or an
    altered declared column drifts it. A declared column absent from the live table (a rename
    or drop — the real footgun) contributes a ``<missing>`` sentinel, so its loss moves the
    hash even if a new column took its place.
    """
    shape: list[tuple[str, list[tuple[str, str]]]] = []
    for table in semantics:
        live = _live_columns(db, table.table)
        # Column names are lowercased in the shape too (not just for lookup) so a purely
        # cosmetic identifier-case difference between the brain and the inspector isn't drift.
        cols = [(c.name.lower(), (live or {}).get(c.name.lower(), _MISSING)) for c in table.columns]
        shape.append((table.table, cols))
    return schema_fingerprint(shape)


def drifted_tables(db: Database, semantics: Sequence[TableSemantics]) -> list[str]:
    """The declared tables that a load-time warning can name as broken — **presence-based**: a
    table missing entirely, or a declared column absent from the live table (a drop/rename, the
    "one thing that silently breaks a shipped agent"). It deliberately does *not* judge type
    changes: naming those reliably needs the eval-time live baseline, not the editable YAML
    type — a live-vs-YAML type compare is the very false-positive :func:`live_schema_fingerprint`
    exists to avoid. Type drift still fires the aggregate warning, just without a named table."""
    drifted: list[str] = []
    for table in semantics:
        live = _live_columns(db, table.table)
        if live is None or any(c.name.lower() not in live for c in table.columns):
            drifted.append(table.table)
    return drifted


def fingerprint_knowledge(knowledge: ProjectKnowledge) -> str:
    """A content hash of the agent's brain — everything that shapes its answers. Two
    runs with the same fingerprint scored the identical context; a release can therefore
    refuse to stamp a held-out score its own files didn't produce (spec §11).

    Deterministic: :class:`ProjectKnowledge` is a pydantic model with stable field order
    and no sets, so its ``model_dump_json`` is a stable serialization of the whole brain.
    """
    return "sha256:" + hashlib.sha256(knowledge.model_dump_json().encode()).hexdigest()
