"""Phase 1.3 — column profiler (spec §3.1, §13).

Exit criteria: profiling the fixture yields correct stats; a large-table path
provably uses sampling, not a full scan; the opt-out suppresses raw sample_values.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sqbyl.introspect import introspect
from sqbyl.profile import ProfileConfig, ProfileOptions, _ProfileSql, profile_table
from sqbyl_runtime.db import Database
from sqbyl_runtime.models import Dialect, TableSemantics


@pytest.fixture
def fixture_tables(duckdb_path: Path) -> dict[str, TableSemantics]:
    with Database.connect(str(duckdb_path), dialect=Dialect.duckdb) as db:
        return {t.table: t for t in introspect(db)}


def _profile(duckdb_path: Path, table: TableSemantics, **kw: object) -> TableSemantics:
    with Database.connect(str(duckdb_path), dialect=Dialect.duckdb) as db:
        return profile_table(db, table, **kw)  # type: ignore[arg-type]


def test_profile_stats_are_correct(
    duckdb_path: Path, fixture_tables: dict[str, TableSemantics]
) -> None:
    orders = _profile(duckdb_path, fixture_tables["analytics.orders"])
    by_name = {c.name: c for c in orders.columns}

    amount = by_name["amount_cents"].profile
    assert amount is not None
    assert amount.nulls == 0.0
    assert amount.min == 127 and amount.max == 310229
    assert amount.p50 is not None and amount.min <= amount.p50 <= amount.max
    assert amount.sampled is False

    status = by_name["status"]
    assert status.profile is not None and status.profile.distinct == 3
    # Low-cardinality column captures top-k values for value-matching.
    assert status.sample_values == ["confirmed", "partial_refund", "refunded"]

    # A date column's min/max is the data's coverage window, as ISO strings.
    created = by_name["created_at"].profile
    assert created is not None
    assert isinstance(created.min, str) and created.min.startswith("2019-")
    assert isinstance(created.max, str) and created.max.startswith("2026-")

    # High-cardinality numeric ids don't get sample_values.
    assert by_name["order_id"].sample_values is None


def test_full_scan_vs_sampling(
    duckdb_path: Path, fixture_tables: dict[str, TableSemantics]
) -> None:
    full = _profile(duckdb_path, fixture_tables["analytics.orders"])
    assert all(c.profile is not None and c.profile.sampled is False for c in full.columns)

    # Drop the row cap below the fixture's 2000 rows: every column must be sampled.
    sampled = _profile(
        duckdb_path,
        fixture_tables["analytics.orders"],
        config=ProfileConfig(sample_over_rows=100, sample_rows=500),
    )
    assert all(c.profile is not None and c.profile.sampled is True for c in sampled.columns)


def test_sampling_clause_is_a_sample_not_a_scan() -> None:
    sql = _ProfileSql(Dialect.duckdb)
    cfg = ProfileConfig(sample_rows=500, sample_seed=7)
    full = sql.from_clause("analytics.orders", sampled=False, cfg=cfg)
    sampled = sql.from_clause("analytics.orders", sampled=True, cfg=cfg)
    # Identifiers are quoted per-dialect (B7) — each part independently, never the
    # whole dotted name as one token.
    assert full == '"analytics"."orders"'
    assert '"analytics"."orders"' in sampled
    assert "USING SAMPLE reservoir(500 ROWS)" in sampled
    assert "REPEATABLE (7)" in sampled  # deterministic across runs


def test_sampling_is_deterministic(
    duckdb_path: Path, fixture_tables: dict[str, TableSemantics]
) -> None:
    cfg = ProfileConfig(sample_over_rows=100, sample_rows=500)
    a = _profile(duckdb_path, fixture_tables["analytics.orders"], config=cfg)
    b = _profile(duckdb_path, fixture_tables["analytics.orders"], config=cfg)
    assert a.model_dump() == b.model_dump()


def test_pii_opt_out(duckdb_path: Path, fixture_tables: dict[str, TableSemantics]) -> None:
    customers = _profile(
        duckdb_path,
        fixture_tables["analytics.customers"],
        options=ProfileOptions(skip={"email"}, suppress_values={"region"}),
    )
    by_name = {c.name: c for c in customers.columns}

    # Fully skipped: no profile at all.
    assert by_name["email"].profile is None
    assert by_name["email"].sample_values is None

    # Stats kept, but raw values suppressed.
    region = by_name["region"]
    assert region.profile is not None and region.profile.distinct == 4
    assert region.sample_values is None

    # A non-suppressed low-cardinality column still captures its values.
    assert by_name["plan"].sample_values == ["free", "pro", "enterprise"]
