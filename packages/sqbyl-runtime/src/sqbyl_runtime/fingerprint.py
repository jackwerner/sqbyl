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
    """The fingerprint of a schema as captured by its :class:`TableSemantics` — the
    shipped brain's own view of the schema it was built against."""
    return schema_fingerprint([(t.table, [(c.name, c.type) for c in t.columns]) for t in semantics])


def fingerprint_knowledge(knowledge: ProjectKnowledge) -> str:
    """A content hash of the agent's brain — everything that shapes its answers. Two
    runs with the same fingerprint scored the identical context; a release can therefore
    refuse to stamp a held-out score its own files didn't produce (spec §11).

    Deterministic: :class:`ProjectKnowledge` is a pydantic model with stable field order
    and no sets, so its ``model_dump_json`` is a stable serialization of the whole brain.
    """
    return "sha256:" + hashlib.sha256(knowledge.model_dump_json().encode()).hexdigest()
