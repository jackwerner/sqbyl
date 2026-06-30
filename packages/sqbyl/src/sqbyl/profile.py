"""Column profiler (spec §3.1, §13, plan 1.3).

Deterministic, read-only, $0: for every column, the stats a human would otherwise
eyeball — null fraction, distinct count, min/max for numerics and dates, a few
percentiles, and top-k values for low-cardinality columns. All plain aggregate SQL,
so it costs zero tokens and runs before any LLM call. The profile is what later
lets the annotator draft descriptions/synonyms *grounded in the data* rather than
guessing from names.

Three disciplines are built in from the start (spec §13):

* **Sampling** — on tables past a row cap we sample (``USING SAMPLE reservoir(...)
  REPEATABLE``) and degrade to approximate stats rather than a full scan; the
  result is flagged ``sampled``.
* **PII opt-out** — ``profile: false`` columns are skipped entirely, and raw
  ``sample_values`` can be suppressed while keeping the non-identifying stats.
* **Read-only** — every query runs through the runtime's read-only ``Database``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import SupportsInt, cast

from sqbyl_runtime.db import Database
from sqbyl_runtime.models import Column, Dialect, Profile, ScalarBound, TableSemantics

_NUMERIC_HINTS = (
    "int",
    "decimal",
    "numeric",
    "double",
    "real",
    "float",
    "hugeint",
)
_TEMPORAL_HINTS = ("date", "timestamp", "time")


@dataclass(frozen=True)
class ProfileConfig:
    """Knobs for the profiler's sampling and top-k behavior."""

    sample_over_rows: int = 5_000_000
    """Tables with more rows than this are profiled over a sample, not a full scan."""
    sample_rows: int = 200_000
    """Reservoir size when sampling."""
    sample_seed: int = 1729
    """Seed for ``REPEATABLE`` sampling so a profile is reproducible across runs."""
    top_k: int = 20
    """How many frequent values to capture for a low-cardinality column."""
    max_distinct_for_top_k: int = 50
    """Only columns with at most this many distinct values get ``sample_values``."""


@dataclass(frozen=True)
class ProfileOptions:
    """Per-column PII controls (driven by ``profile: false`` in the semantic YAML)."""

    skip: set[str] = field(default_factory=set)
    """Column names to leave unprofiled entirely."""
    suppress_values: set[str] = field(default_factory=set)
    """Column names that keep their stats but drop raw ``sample_values``."""


def _classify(sql_type: str) -> str:
    t = sql_type.lower()
    if any(h in t for h in _TEMPORAL_HINTS):
        return "temporal"
    if any(h in t for h in _NUMERIC_HINTS):
        return "numeric"
    return "text"


def _normalize(value: object) -> ScalarBound | None:
    """Coerce a DB scalar into the JSON/YAML-friendly bound the model accepts."""
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass; keep it out of numerics
        return str(value)
    if isinstance(value, int | float):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


class _ProfileSql:
    """Dialect-specific fragments the profiler needs. DuckDB is first-class; Postgres
    is supported on the same shape (untested in CI, no server)."""

    def __init__(self, dialect: Dialect) -> None:
        self.dialect = dialect

    def from_clause(self, table: str, *, sampled: bool, cfg: ProfileConfig) -> str:
        """A FROM-clause source. When sampling, wrap in a subquery so the sample
        binds to the table and the outer WHERE/GROUP BY still apply."""
        if not sampled:
            return table
        if self.dialect is Dialect.duckdb:
            sample = (
                f"SELECT * FROM {table} USING SAMPLE reservoir({cfg.sample_rows} ROWS) "
                f"REPEATABLE ({cfg.sample_seed})"
            )
        else:
            # Postgres: BERNOULLI is row-level and accepts REPEATABLE for determinism.
            sample = (
                f"SELECT * FROM {table} TABLESAMPLE BERNOULLI (1) REPEATABLE ({cfg.sample_seed})"
            )
        return f"({sample}) AS _sqbyl_sample"

    def quantile(self, col: str, q: float) -> str:
        if self.dialect is Dialect.duckdb:
            return f"quantile_cont({col}, {q})"
        return f"percentile_cont({q}) WITHIN GROUP (ORDER BY {col})"


def profile_table(
    db: Database,
    table: TableSemantics,
    *,
    config: ProfileConfig | None = None,
    options: ProfileOptions | None = None,
) -> TableSemantics:
    """Return a copy of ``table`` with each column's ``profile``/``sample_values`` filled.

    Columns in ``options.skip`` are left untouched; columns in
    ``options.suppress_values`` keep their stats but drop raw values.
    """
    cfg = config or ProfileConfig()
    opts = options or ProfileOptions()
    sql = _ProfileSql(db.dialect)

    n_rows = _as_int(db.execute(f"SELECT count(*) FROM {table.table}").rows[0][0])
    sampled = n_rows > cfg.sample_over_rows
    source = sql.from_clause(table.table, sampled=sampled, cfg=cfg)

    profiled_columns = [
        c
        if c.name in opts.skip
        else _profile_column(db, sql, source, c, sampled=sampled, cfg=cfg, opts=opts)
        for c in table.columns
    ]
    return table.model_copy(update={"columns": profiled_columns})


def _profile_column(
    db: Database,
    sql: _ProfileSql,
    source: str,
    column: Column,
    *,
    sampled: bool,
    cfg: ProfileConfig,
    opts: ProfileOptions,
) -> Column:
    kind = _classify(column.type)
    col = column.name

    selects = [
        "count(*) AS n",
        f"count({col}) AS non_null",
        f"count(DISTINCT {col}) AS n_distinct",
    ]
    if kind in ("numeric", "temporal"):
        selects += [f"min({col}) AS mn", f"max({col}) AS mx"]
    if kind == "numeric":
        selects += [f"{sql.quantile(col, q)} AS p{int(q * 100)}" for q in (0.25, 0.50, 0.75, 0.95)]
    row = db.execute(f"SELECT {', '.join(selects)} FROM {source}").dicts()[0]

    n = _as_int(row["n"] or 0)
    if n == 0:
        return column.model_copy(update={"profile": Profile(sampled=sampled)})

    profile = Profile(
        nulls=round((n - _as_int(row["non_null"] or 0)) / n, 6),
        distinct=_as_int(row["n_distinct"] or 0),
        min=_normalize(row.get("mn")),
        max=_normalize(row.get("mx")),
        p25=_as_float(row.get("p25")),
        p50=_as_float(row.get("p50")),
        p75=_as_float(row.get("p75")),
        p95=_as_float(row.get("p95")),
        sampled=sampled,
    )

    sample_values: list[ScalarBound] | None = None
    if (
        column.name not in opts.suppress_values
        and profile.distinct is not None
        and 0 < profile.distinct <= cfg.max_distinct_for_top_k
    ):
        sample_values = _top_k(db, source, col, cfg.top_k)

    return column.model_copy(update={"profile": profile, "sample_values": sample_values})


def _top_k(db: Database, source: str, col: str, k: int) -> list[ScalarBound]:
    """Most frequent non-null values, frequency-desc with a value tiebreak (stable)."""
    rows = db.execute(
        f"SELECT {col} AS v FROM {source} WHERE {col} IS NOT NULL "
        f"GROUP BY {col} ORDER BY count(*) DESC, {col} ASC LIMIT {k}"
    ).rows
    values = [_normalize(r[0]) for r in rows]
    return [v for v in values if v is not None]


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)  # type: ignore[arg-type]


def _as_int(value: object) -> int:
    return int(cast("SupportsInt", value))
