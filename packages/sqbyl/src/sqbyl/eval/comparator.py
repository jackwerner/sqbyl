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

from sqbyl.models.benchmarks import MatchMode
from sqbyl_runtime.db import QueryResult
from sqbyl_runtime.models import Dialect

# A safety cap on the injective column-assignment search in ``columns_superset`` mode: a gold
# result whose columns share value domains can produce many candidate assignments. Past this
# many attempts we stop and report not-equal (conservative → routes to review, never a false
# pass). Real benchmark results are far below it.
_SUPERSET_MAX_ATTEMPTS = 20000

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
    gold: QueryResult,
    generated: QueryResult,
    *,
    float_tol: float = 1e-6,
    match_mode: MatchMode = MatchMode.exact,
) -> Comparison:
    """Order-insensitive, alias-insensitive, numerically-tolerant set comparison.

    ``match_mode`` (spec §7): ``exact`` requires the same columns (by position/value) and the
    same rows. ``columns_superset`` accepts a generated result that reproduces every gold
    column and every gold row but adds *extra* columns — a deliberately weaker bar the
    benchmark author opts into per question (see :class:`~sqbyl.models.benchmarks.MatchMode`).
    """
    ndigits = _tolerance_digits(float_tol)
    if match_mode is MatchMode.columns_superset:
        return _compare_superset(gold, generated, ndigits)
    if len(gold.columns) != len(generated.columns):
        return Comparison(
            equal=False,
            reason=(
                f"column count differs: gold has {len(gold.columns)}, "
                f"generated has {len(generated.columns)}"
            ),
        )
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


def _compare_superset(gold: QueryResult, generated: QueryResult, ndigits: int) -> Comparison:
    """``columns_superset`` match: is gold a column-subset of generated on the same rows?

    True iff there is an **injective** assignment of each gold column to a *distinct* generated
    column such that projecting the generated rows onto those columns (in gold's order)
    reproduces gold's row multiset. This stays alias-insensitive (columns matched by value, not
    name) like ``exact``, and stays row-order insensitive (multiset). Extra generated columns
    are ignored; a *missing* gold column, a wrong value, or a differing row count all fail.
    """
    n_gold, n_gen = len(gold.columns), len(generated.columns)
    if n_gen < n_gold:
        return Comparison(
            equal=False,
            reason=(
                f"generated has fewer columns than gold ({n_gen} < {n_gold}); "
                "a superset match needs every gold column present"
            ),
        )
    gold_rows = [tuple(_canon_cell(v, ndigits) for v in row) for row in gold.rows]
    gen_rows = [tuple(_canon_cell(v, ndigits) for v in row) for row in generated.rows]
    if len(gold_rows) != len(gen_rows):
        return Comparison(
            equal=False,
            reason=(
                f"row count differs: gold {len(gold_rows)} rows, generated {len(gen_rows)} rows"
            ),
        )
    gold_ms = Counter(gold_rows)
    # Necessary pruning condition: gold column i can only map to a generated column whose value
    # *multiset* equals column i's — the marginal must match on both sides of any real projection.
    gold_col_ms = [Counter(r[i] for r in gold_rows) for i in range(n_gold)]
    gen_col_ms = [Counter(r[j] for r in gen_rows) for j in range(n_gen)]
    candidates = [
        [j for j in range(n_gen) if gen_col_ms[j] == gold_col_ms[i]] for i in range(n_gold)
    ]
    for i, cand in enumerate(candidates):
        if not cand:
            return Comparison(
                equal=False,
                reason=f"no generated column reproduces gold column {i} — not a superset",
            )

    attempts = 0

    def _search(assignment: list[int], used: set[int]) -> bool:
        nonlocal attempts
        i = len(assignment)
        if i == n_gold:
            attempts += 1
            projected = Counter(tuple(r[j] for j in assignment) for r in gen_rows)
            return projected == gold_ms
        for j in candidates[i]:
            if j in used or attempts >= _SUPERSET_MAX_ATTEMPTS:
                continue
            assignment.append(j)
            used.add(j)
            if _search(assignment, used):
                return True
            assignment.pop()
            used.discard(j)
        return False

    if _search([], set()):
        extra = n_gen - n_gold
        return Comparison(
            equal=True,
            reason=(
                f"{len(gold_rows)} row(s) match on gold's {n_gold} column(s)"
                + (f"; generated adds {extra} extra column(s)" if extra else "")
            ),
        )
    if attempts >= _SUPERSET_MAX_ATTEMPTS:
        return Comparison(
            equal=False,
            reason="superset search hit its attempt cap without a match — treating as not equal",
        )
    return Comparison(
        equal=False,
        reason="generated does not reproduce gold's rows on any subset of its columns",
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
