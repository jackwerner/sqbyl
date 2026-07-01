"""Result-set comparison + gold-SQL drift normalization (spec §7 Layer 1, §13).

``result_correctness`` is the headline objective scorer: execute gold SQL and generated
SQL, then compare result *sets*. The comparison is

* **row-order insensitive** — rows are compared as a multiset, so ``ORDER BY``
  differences don't matter;
* **alias insensitive** — columns are compared **positionally** by *value*, not by name,
  so ``SELECT SUM(x) AS revenue`` and ``SELECT SUM(x) AS total`` match;
* **numerically tolerant** — numbers are quantized to a tolerance, so ``100`` (int),
  ``100.0`` (float) and ``Decimal('100.00')`` are equal, as are values that differ only
  past the tolerance (rounding, float noise).

Columns are aligned **by position**, not by content signature. Content-signature sorting
would make column *order* insensitive too — but that silently equates a gold
``(active=100, inactive=5)`` with a swapped, semantically-wrong ``(active=5,
inactive=100)`` whenever two columns share a value domain, inflating accuracy. Layer 1
stays strict; a defensible column *reorder* ("different SQL, same intent") is exactly what
the Layer-2 ``semantic_equivalence`` judge exists to catch (spec §7).

It also handles **gold-SQL drift**: a gold answer written with ``now()`` /
``current_date`` would score differently every day. :func:`normalize_as_of` rewrites
those to a fixed as-of literal so a relative-window question scores **stably** — and the
same as-of is applied to *both* gold and generated SQL, so a relative query compares
fairly (spec §13).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from sqbyl_runtime.db import QueryResult
from sqbyl_runtime.models import Dialect

# Function names that resolve to the wall-clock "now" and so make gold drift over time.
_NOW_TIMESTAMP_FNS = {"now", "current_timestamp", "getdate", "sysdate", "current_time"}
_NOW_DATE_FNS = {"current_date", "today", "curdate"}


def _sqlglot_dialect(dialect: Dialect) -> str:
    """Map a sqbyl dialect to sqlglot's reader name (sqbyl 'postgresql' → 'postgres')."""
    return "postgres" if dialect is Dialect.postgresql else dialect.value


def normalize_as_of(sql: str, *, as_of: datetime | None, dialect: Dialect) -> str:
    """Rewrite ``now()`` / ``current_timestamp`` / ``current_date`` to a fixed literal.

    Returns ``sql`` unchanged when ``as_of`` is ``None`` or the SQL doesn't parse (an
    unparseable statement is left for the DB to reject downstream). Freezing the clock
    is what makes a ``now()``-relative gold answer score the same regardless of the wall
    clock — the comparator's defense against gold-SQL drift (spec §13).
    """
    if as_of is None:
        return sql
    read = _sqlglot_dialect(dialect)
    ts_literal = exp.cast(exp.Literal.string(as_of.strftime("%Y-%m-%d %H:%M:%S")), to="TIMESTAMP")
    date_literal = exp.cast(exp.Literal.string(as_of.strftime("%Y-%m-%d")), to="DATE")

    def _freeze(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.CurrentDate):
            return date_literal.copy()
        if isinstance(node, exp.CurrentTimestamp | exp.CurrentTime):
            return ts_literal.copy()
        # `now()` and friends may parse to an anonymous function rather than a typed node.
        if isinstance(node, exp.Anonymous):
            fn = node.name.lower()
            if fn in _NOW_DATE_FNS:
                return date_literal.copy()
            if fn in _NOW_TIMESTAMP_FNS:
                return ts_literal.copy()
        return node

    try:
        tree = sqlglot.parse_one(sql, read=read)
    except SqlglotError:
        return sql
    return tree.transform(_freeze).sql(dialect=read)


@dataclass(frozen=True)
class Comparison:
    """Whether two result sets are equal, with a human-readable reason."""

    equal: bool
    reason: str


def compare_result_sets(
    gold: QueryResult, generated: QueryResult, *, float_tol: float = 1e-6
) -> Comparison:
    """Order-insensitive, alias-insensitive, numerically-tolerant set comparison."""
    if len(gold.columns) != len(generated.columns):
        return Comparison(
            equal=False,
            reason=(
                f"column count differs: gold has {len(gold.columns)}, "
                f"generated has {len(generated.columns)}"
            ),
        )
    ndigits = _tolerance_digits(float_tol)
    gold_ms = _canonical_multiset(gold, ndigits)
    gen_ms = _canonical_multiset(generated, ndigits)
    if gold_ms == gen_ms:
        return Comparison(equal=True, reason=f"{len(gold.rows)} row(s) match")
    only_gold = gold_ms - gen_ms
    only_gen = gen_ms - gold_ms
    return Comparison(
        equal=False,
        reason=(
            f"row sets differ: {sum(only_gold.values())} only in gold, "
            f"{sum(only_gen.values())} only in generated "
            f"(gold {len(gold.rows)} rows, generated {len(generated.rows)} rows)"
        ),
    )


def _tolerance_digits(float_tol: float) -> int:
    """Decimal places implied by a tolerance (1e-6 → 6). Clamped to a sane range."""
    if float_tol <= 0:
        return 12
    return max(0, min(12, round(-math.log10(float_tol))))


def _canonical_multiset(result: QueryResult, ndigits: int) -> Counter[tuple[object, ...]]:
    """Canonicalize a result into a row multiset, comparing columns **by position**."""
    return Counter(tuple(_canon_cell(v, ndigits) for v in row) for row in result.rows)


def _canon_cell(value: object, ndigits: int) -> object:
    """Normalize one cell to its equivalence class. The contract, by type:

    * ``None`` and ``bool`` — identity (``None`` is distinct from ``0``/``''``);
    * numbers (``int``/``float``/``Decimal``) — quantized to ``ndigits`` and integer-valued
      numbers collapsed, so ``100 == 100.0 == Decimal('100.00')`` and sub-tolerance noise
      is absorbed;
    * ``datetime``/``date`` — ISO string (so an ``as_of`` normalization compares cleanly);
    * ``str`` — outer whitespace trimmed only; case and internal whitespace are
      **significant** ("US" ≠ "us"). Text equivalence is deliberately strict at Layer 1 —
      a fuzzy string match is a Layer-2 judge call, not a deterministic one.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int | float | Decimal):
        rounded = round(float(value), ndigits)
        return float(int(rounded)) if rounded == int(rounded) else rounded
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, str):
        return value.strip()
    return str(value)
