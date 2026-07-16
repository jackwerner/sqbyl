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
from math import ceil, floor
from typing import SupportsInt, cast

from sqlglot import exp

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

# The percentiles captured for numeric columns.
_QUANTILES = (0.25, 0.50, 0.75, 0.95)

# Numbers-stored-as-text detection (finding B12): probe this many non-null values of a
# text-typed column; flag it if at least this fraction parse as numbers, and take the numeric
# min/max off the same sample so annotate sees the *magnitude* (a district with values up to
# ~1.2M is population, not area in km²) — the flag alone doesn't disambiguate. Bounded so it
# stays a cheap $0 add on top of the stats query; the min/max are sample-based (approximate),
# consistent with the profiler's other sampled stats.
_NUMERIC_TEXT_PROBE = 1000
_NUMERIC_TEXT_MIN_SAMPLE = 10
_NUMERIC_TEXT_FRACTION = 0.98


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
    """Dialect-specific SQL fragments the profiler needs.

    DuckDB and SQLite are exercised in CI (DuckDB first-class; SQLite via the bundled
    fixture). Postgres/MySQL/Snowflake/BigQuery use documented syntax but run only
    against a live server. Dialects with no in-SQL continuous-percentile aggregate
    (SQLite, MySQL) report ``quantiles_in_sql = False``; the profiler then computes
    percentiles in Python from the column's values instead of embedding them in SQL.
    """

    # Dialects with an in-SQL continuous-percentile aggregate we can embed directly in
    # the stats query. Everything else falls back to a Python computation.
    _QUANTILE_IN_SQL = frozenset(
        {Dialect.duckdb, Dialect.postgresql, Dialect.snowflake, Dialect.bigquery}
    )

    def __init__(self, dialect: Dialect) -> None:
        self.dialect = dialect
        # sqbyl uses 'postgresql'; sqlglot's dialect is named 'postgres'. The rest match.
        self._sqlglot_name = "postgres" if dialect is Dialect.postgresql else dialect.value

    @property
    def quantiles_in_sql(self) -> bool:
        return self.dialect in self._QUANTILE_IN_SQL

    def quote(self, ident: str) -> str:
        """Quote a single identifier for this dialect. Every column/table name the
        profiler interpolates into SQL goes through here — real-world schemas (e.g.
        BIRD's ``Charter School (Y/N)``) carry spaces and parentheses that break
        unquoted SQL and the read-only guard's parser alike."""
        return exp.to_identifier(ident, quoted=True).sql(dialect=self._sqlglot_name)

    def quote_table(self, qualified: str) -> str:
        """Quote a ``schema.name`` table reference, quoting each part independently."""
        schema, dot, name = qualified.partition(".")
        if not dot:  # unqualified; quote the whole thing as one identifier
            return self.quote(schema)
        table = exp.Table(
            this=exp.to_identifier(name, quoted=True),
            db=exp.to_identifier(schema, quoted=True),
        )
        return table.sql(dialect=self._sqlglot_name)

    def from_clause(self, table: str, *, sampled: bool, cfg: ProfileConfig) -> str:
        """A FROM-clause source (the table name already quoted). When sampling, wrap in
        a subquery so the sample binds to the table and the outer WHERE/GROUP BY apply."""
        quoted = self.quote_table(table)
        if not sampled:
            return quoted
        return f"({self._sample_select(quoted, cfg)}) AS _sqbyl_sample"

    def _sample_select(self, table: str, cfg: ProfileConfig) -> str:
        """A row-sample SELECT over an already-quoted ``table`` reference. Only used past
        ``sample_over_rows``; each dialect gets a deterministic-where-possible form
        (untested dialects are documented best-effort)."""
        seed = cfg.sample_seed
        if self.dialect is Dialect.duckdb:
            return (
                f"SELECT * FROM {table} USING SAMPLE reservoir({cfg.sample_rows} ROWS) "
                f"REPEATABLE ({seed})"
            )
        if self.dialect is Dialect.sqlite:
            # No TABLESAMPLE and no seedable RAND; hash the rowid for a deterministic
            # pseudo-random subset.
            return (
                f"SELECT * FROM {table} "
                f"ORDER BY ((rowid * 2654435761 + {seed}) % 2147483647) "
                f"LIMIT {cfg.sample_rows}"
            )
        if self.dialect is Dialect.mysql:
            # No TABLESAMPLE; a seeded RAND ordering gives a reproducible sample.
            return f"SELECT * FROM {table} ORDER BY RAND({seed}) LIMIT {cfg.sample_rows}"
        if self.dialect is Dialect.bigquery:
            # No REPEATABLE; SYSTEM (block-level) sampling is the documented form.
            return f"SELECT * FROM {table} TABLESAMPLE SYSTEM (1 PERCENT)"
        if self.dialect is Dialect.snowflake:
            return f"SELECT * FROM {table} SAMPLE (1)"
        # Postgres: BERNOULLI is row-level and accepts REPEATABLE for determinism.
        return f"SELECT * FROM {table} TABLESAMPLE BERNOULLI (1) REPEATABLE ({seed})"

    def quantile(self, col: str, q: float) -> str:
        """A continuous-percentile SQL expression (only for ``quantiles_in_sql`` dialects)."""
        if self.dialect is Dialect.duckdb:
            return f"quantile_cont({col}, {q})"
        if self.dialect is Dialect.bigquery:
            return f"APPROX_QUANTILES({col}, 100)[OFFSET({int(round(q * 100))})]"
        # Postgres and Snowflake ordered-set aggregate.
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

    n_rows = _as_int(db.execute(f"SELECT count(*) FROM {sql.quote_table(table.table)}").rows[0][0])
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
    col = sql.quote(column.name)

    selects = [
        "count(*) AS n",
        f"count({col}) AS non_null",
        f"count(DISTINCT {col}) AS n_distinct",
    ]
    if kind in ("numeric", "temporal"):
        selects += [f"min({col}) AS mn", f"max({col}) AS mx"]
    want_quantiles = kind == "numeric"
    if want_quantiles and sql.quantiles_in_sql:
        selects += [f"{sql.quantile(col, q)} AS p{int(q * 100)}" for q in _QUANTILES]
    row = db.execute(f"SELECT {', '.join(selects)} FROM {source}").dicts()[0]

    n = _as_int(row["n"] or 0)
    if n == 0:
        return column.model_copy(update={"profile": Profile(sampled=sampled)})

    # Dialects without an in-SQL percentile aggregate (SQLite, MySQL) compute the
    # quantiles in Python from the column's values; the rest read them off the row.
    if want_quantiles and not sql.quantiles_in_sql:
        pctl = _quantiles_via_python(db, source, col)
    else:
        pctl = {q: _as_float(row.get(f"p{int(q * 100)}")) for q in _QUANTILES}

    # A text-declared column whose values are all numeric (B12): flag it so annotate can
    # surface it and the agent can CAST — and take min/max off the numeric sample so the
    # *magnitude* (the disambiguating signal) reaches the annotator, since text columns get
    # no SQL min/max above.
    numtext = _numeric_text_probe(db, source, col) if kind == "text" else _NumericText(False)
    mn = (
        _normalize(row.get("mn"))
        if kind in ("numeric", "temporal")
        else _normalize(numtext.minimum)
    )
    mx = (
        _normalize(row.get("mx"))
        if kind in ("numeric", "temporal")
        else _normalize(numtext.maximum)
    )

    profile = Profile(
        nulls=round((n - _as_int(row["non_null"] or 0)) / n, 6),
        distinct=_as_int(row["n_distinct"] or 0),
        min=mn,
        max=mx,
        p25=pctl[0.25],
        p50=pctl[0.50],
        p75=pctl[0.75],
        p95=pctl[0.95],
        sampled=sampled,
        numeric_text=numtext.is_numeric_text,
    )

    sample_values: list[ScalarBound] | None = None
    if (
        column.name not in opts.suppress_values
        and profile.distinct is not None
        and 0 < profile.distinct <= cfg.max_distinct_for_top_k
    ):
        sample_values = _top_k(db, source, col, cfg.top_k)

    return column.model_copy(update={"profile": profile, "sample_values": sample_values})


@dataclass(frozen=True)
class _NumericText:
    """Result of the numbers-stored-as-text probe: the flag plus the sample's numeric range."""

    is_numeric_text: bool
    minimum: float | None = None
    maximum: float | None = None


def _numeric_text_probe(db: Database, source: str, col: str) -> _NumericText:
    """Whether a text-typed column actually holds numbers, and its numeric min/max (finding B12).

    Probes a bounded sample of non-null values: flags the column when nearly all parse as
    numbers (the numbers-stored-as-text pattern common in dumped/dynamically-typed schemas), and
    returns the min/max of those numbers so annotate sees the magnitude — the flag alone doesn't
    tell "area" from "population". Bounded and read-only, so it stays a cheap add on the stats
    query; the range is sample-based (approximate)."""
    rows = db.execute(
        f"SELECT {col} AS v FROM {source} WHERE {col} IS NOT NULL LIMIT {_NUMERIC_TEXT_PROBE}"
    ).rows
    values = [r[0] for r in rows if r[0] is not None]
    if len(values) < _NUMERIC_TEXT_MIN_SAMPLE:
        return _NumericText(False)
    # Only *strings* that parse as numbers count — a column already returning ints/floats is
    # numeric by type, not numbers-stored-as-text.
    nums = [f for v in values if isinstance(v, str) and (f := _as_float(v)) is not None]
    if len(nums) / len(values) < _NUMERIC_TEXT_FRACTION:
        return _NumericText(False)
    return _NumericText(True, min(nums), max(nums))


def _top_k(db: Database, source: str, col: str, k: int) -> list[ScalarBound]:
    """Most frequent non-null values, frequency-desc with a value tiebreak (stable)."""
    rows = db.execute(
        f"SELECT {col} AS v FROM {source} WHERE {col} IS NOT NULL "
        f"GROUP BY {col} ORDER BY count(*) DESC, {col} ASC LIMIT {k}"
    ).rows
    values = [_normalize(r[0]) for r in rows]
    return [v for v in values if v is not None]


def _quantiles_via_python(db: Database, source: str, col: str) -> dict[float, float | None]:
    """Continuous percentiles computed in Python, for dialects with no percentile
    aggregate (SQLite, MySQL). Pulls the column's non-null values (bounded by sampling)
    and interpolates — matching what ``percentile_cont`` returns on the same input."""
    rows = db.execute(
        f"SELECT {col} AS v FROM {source} WHERE {col} IS NOT NULL ORDER BY {col}"
    ).rows
    values = [f for r in rows if (f := _as_float(r[0])) is not None]
    return {q: _percentile_cont(values, q) for q in _QUANTILES}


def _percentile_cont(sorted_values: list[float], q: float) -> float | None:
    """Linear-interpolation percentile over pre-sorted values, matching SQL
    ``percentile_cont`` (continuous, interpolated between the two nearest ranks)."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(floor(pos))
    hi = int(ceil(pos))
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] * (hi - pos) + sorted_values[hi] * (pos - lo)


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    # SQLite/MySQL are dynamically typed: a column classified numeric can still hold
    # ``''`` or other non-numeric junk for "missing". The in-SQL percentile path
    # (DuckDB/Postgres) coerces/ignores those; the Python path must match that
    # tolerance rather than crash the whole `profile` command (B8).
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int:
    return int(cast("SupportsInt", value))
