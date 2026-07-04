"""Importers — seed a project from SQL people already wrote (spec §12 M4, plan 9.4).

The fastest way to a good benchmark and a real join graph is the SQL an org *already*
runs: dbt models, a query log, existing view definitions. Each is a source of
**execution-grounded** gold SQL (it ran in production) and **observed joins** (a join
seen in a real query is very likely a real relationship). This module turns those into:

* **proposed examples** — a :class:`~sqbyl.models.candidates.Candidate` per query, executed
  against the live DB (dropped if it doesn't run, exactly like ``synth``) so a human does a
  yes/no pass in the review console instead of authoring from scratch; and
* **proposed joins** — :class:`ProposedJoin` edges parsed from each query's ``ON`` clauses,
  deduped across the corpus (a join seen in more queries scores higher).

All deterministic and **$0** — no LLM. The one thing an importer can't invent is the
natural-language *question* for an example: it uses the source's own label (a dbt model
name, a view name, a log comment) when there is one — tagged ``derived-question`` since a
name isn't a verified description — and otherwise flags the candidate ``needs-question``
for the human to fill. Provenance (which source seeded it) rides along so review can weigh it.

**Imported SQL can carry literals (spec §13).** Unlike synth's LLM-authored gold, a query
log or view body is production SQL that may bake in literal values (``WHERE email = '…'``) —
which would cross into the git-committed ``dev.yaml`` (and prompts) on accept. Candidates
whose ``gold_sql`` contains a string literal are tagged ``contains-literals`` so ``sqbyl
review`` surfaces them to redact/parameterize before accepting; the import CLI warns too.

Dev-only (it writes the review queue and reads semantics), so it lives in ``sqbyl``. It
never touches ``test.yaml`` — imported candidates flow to ``dev.yaml`` on accept like every
other candidate (invariant 3).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from pathlib import Path

import sqlglot
from pydantic import Field
from sqlglot import exp
from sqlglot.errors import SqlglotError

from sqbyl.models.candidates import Candidate, DroppedCandidate, DropReason, ExecutionEvidence
from sqbyl_runtime.db import (
    Database,
    StaticValidationError,
    UnparseableSqlError,
    WriteAttemptError,
)
from sqbyl_runtime.models import Dialect, Join, JoinCardinality, SqbylModel

# sqlglot's Postgres reader is named 'postgres'; every other sqbyl dialect matches its value.
_SQLGLOT_NAME = {Dialect.postgresql: "postgres"}
# Statement roots that are a pure read (a query log may also contain writes we ignore).
_QUERY_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)


def _read_dialect(dialect: Dialect) -> str:
    return _SQLGLOT_NAME.get(dialect, dialect.value)


# A join we only *observed* in a query — real cardinality is unknown, so it lands as a
# low-confidence, review-me candidate rather than a confident FK-grade edge. More queries
# using the same edge nudges confidence up (evidence accumulates), capped well under 1.0.
_OBSERVED_JOIN_BASE_CONFIDENCE = 0.4
_OBSERVED_JOIN_PER_HIT = 0.1
_OBSERVED_JOIN_MAX_CONFIDENCE = 0.9
_OBSERVED_JOIN_CARDINALITY: JoinCardinality = "many_to_many"


class QueryInput(SqbylModel):
    """One SQL statement to import, plus an optional human-readable label (the dbt model
    name, view name, or log comment) used to seed the example's question."""

    sql: str
    label: str | None = None
    source: str = "query"


class ProposedJoin(SqbylModel):
    """A join edge observed in imported SQL — a candidate for a table's ``joins:`` block.

    Directionless by nature (it came from an ``ON`` equality), so ``from_table``/``to_table``
    are just the two sides; a human sets the real cardinality in review. ``hits`` is how many
    imported queries used this edge — the evidence behind ``confidence``.
    """

    from_table: str
    to_table: str
    on: str
    type: JoinCardinality = _OBSERVED_JOIN_CARDINALITY
    confidence: float = _OBSERVED_JOIN_BASE_CONFIDENCE
    hits: int = 1
    source: str = "query"

    def as_join(self) -> Join:
        """The runtime :class:`Join` shape to write under ``from_table`` in semantics."""
        return Join(to=self.to_table, type=self.type, on=self.on, confidence=self.confidence)


class ImportResult(SqbylModel):
    """What an import produced: executed example candidates, deduped joins, and drops."""

    candidates: list[Candidate] = Field(default_factory=list)
    joins: list[ProposedJoin] = Field(default_factory=list)
    dropped: list[DroppedCandidate] = Field(default_factory=list)

    @property
    def n_candidates(self) -> int:
        return len(self.candidates)

    @property
    def n_joins(self) -> int:
        return len(self.joins)


# --- join extraction (deterministic) ---------------------------------------------


def extract_joins(sql: str, *, dialect: Dialect, source: str = "query") -> list[ProposedJoin]:
    """Parse ``sql`` and return a :class:`ProposedJoin` for each ``JOIN … ON`` edge.

    Reads the two tables straight from each ``ON`` equality's column qualifiers (resolving
    aliases to real table names), so it's robust to multi-join queries. Unparseable SQL
    yields no joins (fails soft — this is a best-effort seed, not a gate).
    """
    try:
        tree = sqlglot.parse_one(sql, read=_read_dialect(dialect))
    except SqlglotError:
        return []
    if not isinstance(tree, exp.Expression):
        return []

    alias_map = _alias_to_table(tree)
    joins: list[ProposedJoin] = []
    seen: set[tuple[str, str, str]] = set()
    for join in tree.find_all(exp.Join):
        on = join.args.get("on")
        if on is None:
            continue
        tables = _tables_in_condition(on, alias_map)
        if len(tables) < 2:
            continue
        a, b = tables[0], tables[1]
        on_sql = on.sql(dialect=_read_dialect(dialect))
        key = (a, b, _norm(on_sql))
        if key in seen:
            continue
        seen.add(key)
        joins.append(ProposedJoin(from_table=a, to_table=b, on=on_sql, source=source))
    return joins


def _alias_to_table(tree: exp.Expression) -> dict[str, str]:
    """Map every table alias (and bare name) in the statement to its qualified table name."""
    mapping: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        qualified = ".".join(p for p in (table.catalog, table.db, table.name) if p)
        mapping[table.name] = qualified
        if table.alias:
            mapping[table.alias] = qualified
    return mapping


def _tables_in_condition(condition: exp.Expression, alias_map: dict[str, str]) -> list[str]:
    """The distinct real tables referenced by the qualified columns in an ON condition."""
    tables: list[str] = []
    for col in condition.find_all(exp.Column):
        qualifier = col.table
        if qualifier and qualifier in alias_map:
            resolved = alias_map[qualifier]
            if resolved not in tables:
                tables.append(resolved)
    return tables


def _dedup_joins(joins: Sequence[ProposedJoin]) -> list[ProposedJoin]:
    """Collapse the same edge seen across many queries; bump confidence by frequency."""
    merged: dict[tuple[str, str, str], ProposedJoin] = {}
    for join in joins:
        pair = tuple(sorted((join.from_table, join.to_table)))
        key = (pair[0], pair[1], _norm(join.on))
        existing = merged.get(key)
        if existing is None:
            merged[key] = join.model_copy()
        else:
            existing.hits += 1
            existing.confidence = min(
                _OBSERVED_JOIN_MAX_CONFIDENCE,
                _OBSERVED_JOIN_BASE_CONFIDENCE + _OBSERVED_JOIN_PER_HIT * existing.hits,
            )
    return list(merged.values())


# --- importing a corpus of queries -----------------------------------------------


def import_queries(inputs: Sequence[QueryInput], *, db: Database, dialect: Dialect) -> ImportResult:
    """Execution-ground each query into an example candidate and collect its joins.

    A query that fails static validation or execution is dropped (with a reason), never
    surfaced — same execution-grounding bar as ``synth`` (spec §6.A). Joins are collected
    from every input (even dropped ones can contribute a real edge) and deduped.
    """
    candidates: list[Candidate] = []
    dropped: list[DroppedCandidate] = []
    all_joins: list[ProposedJoin] = []

    for item in inputs:
        all_joins.extend(extract_joins(item.sql, dialect=dialect, source=item.source))
        evidence, reason, detail = _ground(item.sql, db=db)
        if evidence is None:
            assert reason is not None
            dropped.append(
                DroppedCandidate(
                    question=_question_for(item), gold_sql=item.sql, reason=reason, detail=detail
                )
            )
            continue
        candidates.append(_candidate_for(item, evidence, dialect=dialect))

    return ImportResult(candidates=candidates, joins=_dedup_joins(all_joins), dropped=dropped)


def _ground(
    sql: str, *, db: Database
) -> tuple[ExecutionEvidence | None, DropReason | None, str | None]:
    """Static-validate + execute one query; return its evidence or why it was dropped."""
    try:
        db.explain(sql)
    except (StaticValidationError, WriteAttemptError, UnparseableSqlError) as exc:
        # UnparseableSqlError is reachable via queries_from_views (raw view bodies aren't
        # pre-parsed), so it must soft-drop here rather than crash the whole import run.
        return None, DropReason.syntax_error, str(exc)
    try:
        result = db.execute(sql)
    except WriteAttemptError as exc:
        return None, DropReason.syntax_error, str(exc)
    except Exception as exc:  # noqa: BLE001 - any execution error drops the candidate
        return None, DropReason.execution_error, str(exc)
    if len(result.rows) == 0:
        return None, DropReason.empty_result, None
    return ExecutionEvidence.from_result(result), None, None


def _candidate_for(item: QueryInput, evidence: ExecutionEvidence, *, dialect: Dialect) -> Candidate:
    question, tags = _question_and_tags(item)
    # Imported SQL (esp. query logs) can carry literal values baked into predicates —
    # possibly PII — which would cross into the git-committed dev.yaml on accept. Flag it so
    # `sqbyl review` surfaces it for a human to redact/parameterize (responsible-ai).
    if _has_string_literal(item.sql, dialect=dialect):
        tags.append("contains-literals")
    return Candidate(
        id=_candidate_id(item),
        question=question,
        gold_sql=item.sql,
        tags=tags,
        seed=f"import:{item.source}",
        evidence=evidence,
    )


def _question_and_tags(item: QueryInput) -> tuple[str, list[str]]:
    """A question from the source's label, else a placeholder flagged for the human.

    A label-derived question is a mechanical transliteration of a name (a dbt stem, a view
    name), not a verified statement of what the SQL answers — so it's tagged
    ``derived-question`` (not presented as authored). No label ⇒ an honest ``[needs question]``
    placeholder + ``needs-question`` rather than a fabricated one.
    """
    if item.label and item.label.strip():
        return _humanize(item.label), ["import", "derived-question"]
    return f"[needs question] describe what this query answers ({item.source})", [
        "import",
        "needs-question",
    ]


def _has_string_literal(sql: str, *, dialect: Dialect) -> bool:
    """Whether the SQL contains any string literal (the PII-carrying shape, e.g. an email in
    a WHERE clause). Best-effort: unparseable SQL is treated as no-flag."""
    try:
        tree = sqlglot.parse_one(sql, read=_read_dialect(dialect))
    except SqlglotError:
        return False
    if not isinstance(tree, exp.Expression):
        return False
    return any(literal.is_string for literal in tree.find_all(exp.Literal))


def _question_for(item: QueryInput) -> str:
    return _question_and_tags(item)[0]


def _humanize(label: str) -> str:
    """A dbt/view name → a rough question seed: 'stg_orders_by_region' → 'stg orders by region'."""
    words = re.sub(r"[_\-.]+", " ", label).strip()
    return f"Show {words}"


def _candidate_id(item: QueryInput) -> str:
    digest = hashlib.sha256(item.sql.encode()).hexdigest()[:8]
    stem = re.sub(r"[^a-z0-9]+", "-", (item.label or item.source).lower()).strip("-")
    return f"import-{stem or 'q'}-{digest}"


# --- sources ---------------------------------------------------------------------


def split_sql_statements(
    text: str, *, dialect: Dialect, source: str = "query-log"
) -> list[QueryInput]:
    """Split a SQL blob (a query log or a ``.sql`` file) into individual SELECT inputs.

    Uses sqlglot to split on statement boundaries (so a ``;`` inside a string doesn't
    mis-split), keeps only queries (a log may contain writes we ignore), and lifts a
    leading ``-- comment`` on a statement into its label.
    """
    try:
        statements = sqlglot.parse(text, read=_read_dialect(dialect))
    except SqlglotError:
        return []
    inputs: list[QueryInput] = []
    for stmt in statements:
        if stmt is None or not isinstance(stmt, _QUERY_ROOTS):
            continue  # ignore non-SELECT statements in the log
        comments = stmt.comments or []
        label = comments[0].strip() if comments else None
        inputs.append(
            QueryInput(sql=stmt.sql(dialect=_read_dialect(dialect)), label=label, source=source)
        )
    return inputs


def queries_from_dbt(compiled_dir: str | Path, *, dialect: Dialect) -> list[QueryInput]:
    """Read a dbt project's **compiled** SQL (``target/compiled/**/*.sql``) as import inputs.

    Compiled dbt SQL is plain SQL (Jinja already resolved), so it parses and runs directly;
    the model name (the file stem) becomes the label. Point this at ``target/compiled`` — the
    raw ``models/`` still contain ``{{ ref() }}`` Jinja that isn't valid SQL.
    """
    root = Path(compiled_dir)
    inputs: list[QueryInput] = []
    for path in sorted(root.rglob("*.sql")):
        sql = path.read_text().strip()
        if sql:
            inputs.append(QueryInput(sql=sql, label=path.stem, source=f"dbt:{path.stem}"))
    return inputs


def queries_from_views(db: Database) -> list[QueryInput]:
    """Read existing view definitions from the live database as import inputs."""
    from sqlalchemy import inspect

    inspector = inspect(db.engine)
    inputs: list[QueryInput] = []
    for schema in [None, *(_safe_schemas(inspector))]:
        for view in inspector.get_view_names(schema=schema):
            definition = (inspector.get_view_definition(view, schema=schema) or "").strip()
            if definition:
                inputs.append(QueryInput(sql=definition, label=view, source=f"view:{view}"))
    return inputs


def _safe_schemas(inspector: object) -> list[str]:
    try:
        return list(inspector.get_schema_names())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - some dialects/permissions can't list schemas
        return []


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().lower()
