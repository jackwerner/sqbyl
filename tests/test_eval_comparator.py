"""Phase 3.2 — the result-set comparator and gold-SQL drift normalization (spec §7, §13)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqbyl.eval.comparator import compare_result_sets, normalize_as_of
from sqbyl_runtime.db import QueryResult
from sqbyl_runtime.models import Dialect


def _qr(columns: list[str], rows: list[tuple[object, ...]]) -> QueryResult:
    return QueryResult(columns=columns, rows=rows)


def test_identical_result_sets_match() -> None:
    a = _qr(["n"], [(3,)])
    assert compare_result_sets(a, a).equal


def test_row_order_is_ignored() -> None:
    gold = _qr(["region", "n"], [("us", 2), ("emea", 1)])
    gen = _qr(["region", "n"], [("emea", 1), ("us", 2)])
    assert compare_result_sets(gold, gen).equal


def test_column_aliases_ignored_but_position_respected() -> None:
    # Same column order and values, different aliases → match (compared by value).
    gold = _qr(["region", "revenue"], [("us", 100), ("emea", 50)])
    gen = _qr(["reg", "rev"], [("us", 100), ("emea", 50)])
    assert compare_result_sets(gold, gen).equal


def test_swapped_same_domain_columns_do_not_falsely_match() -> None:
    # active/inactive counts swapped: a genuinely wrong answer that a content-signature
    # column sort would falsely equate. Positional comparison keeps it a mismatch.
    gold = _qr(["active", "inactive"], [(100, 5)])
    gen = _qr(["active", "inactive"], [(5, 100)])
    assert not compare_result_sets(gold, gen).equal


def test_numeric_tolerance_and_cross_type_equality() -> None:
    # int vs float vs Decimal, and a sub-tolerance difference, all compare equal.
    gold = _qr(["v"], [(100,)])
    gen = _qr(["v"], [(Decimal("100.0000001"),)])
    assert compare_result_sets(gold, gen).equal
    gen_float = _qr(["v"], [(100.0,)])
    assert compare_result_sets(gold, gen_float).equal


def test_genuine_mismatch_is_reported() -> None:
    gold = _qr(["n"], [(3,)])
    gen = _qr(["n"], [(4,)])
    result = compare_result_sets(gold, gen)
    assert not result.equal
    assert "differ" in result.reason


def test_column_count_mismatch_is_reported() -> None:
    result = compare_result_sets(_qr(["a"], [(1,)]), _qr(["a", "b"], [(1, 2)]))
    assert not result.equal
    assert "column count" in result.reason


def test_duplicate_rows_are_a_multiset_not_a_set() -> None:
    gold = _qr(["x"], [(1,), (1,), (2,)])
    gen = _qr(["x"], [(1,), (2,), (2,)])  # same set {1,2}, different multiplicities
    assert not compare_result_sets(gold, gen).equal


def test_cell_equivalence_contract() -> None:
    # Whitespace trimmed → equal; case significant → not equal.
    assert compare_result_sets(_qr(["s"], [(" us ",)]), _qr(["s"], [("us",)])).equal
    assert not compare_result_sets(_qr(["s"], [("US",)]), _qr(["s"], [("us",)])).equal
    # None is distinct from empty string and from 0.
    assert not compare_result_sets(_qr(["v"], [(None,)]), _qr(["v"], [("",)])).equal
    assert not compare_result_sets(_qr(["v"], [(None,)]), _qr(["v"], [(0,)])).equal
    # Decimal trailing zeros collapse to the same numeric value.
    assert compare_result_sets(_qr(["v"], [(Decimal("100.00"),)]), _qr(["v"], [(100,)])).equal


def test_normalize_as_of_rewrites_now_to_a_fixed_literal() -> None:
    as_of = datetime(2026, 6, 30, 12, 0, 0)
    out = normalize_as_of(
        "SELECT * FROM orders WHERE created_at >= now()", as_of=as_of, dialect=Dialect.duckdb
    )
    assert "2026-06-30" in out
    assert "NOW(" not in out.upper()


def test_normalize_as_of_is_noop_without_as_of() -> None:
    sql = "SELECT count(*) FROM orders WHERE created_at >= now()"
    assert normalize_as_of(sql, as_of=None, dialect=Dialect.duckdb) == sql
