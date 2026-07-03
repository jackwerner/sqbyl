"""Rolling up the operational KPI report (spec §7.5).

``build_report`` reads only what sqbyl already stored — ``.sqbyl/usage.db`` (cost),
``.sqbyl/runs/`` (quality + per-query latency) — and folds it into a :class:`KpiReport`.
It is a **pure reporting view**: it spends no tokens, opens no connection to the user's
database, and emits aggregates only, never row data (§13). Cost and quality are computed
**separately for dev and the held-out test set** and never conflated (spec §7).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqbyl.models.kpis import KpiReport, PerformanceKpis, QualityKpis, UnitEconomics
from sqbyl.stats import SMALL_N_FLOOR, percentile
from sqbyl_runtime.state.layout import SqbylPaths

if TYPE_CHECKING:
    from sqbyl.models.runs import ScoredRun
    from sqbyl.project import Project
    from sqbyl_runtime.state.usage import UsageRecord


def _load_usage(paths: SqbylPaths) -> list[UsageRecord]:
    from sqbyl_runtime.state.usage import UsageStore

    if not paths.usage_db.exists():
        return []
    with UsageStore(paths.usage_db) as store:
        return store.all()


def _unit_economics(
    rows: list[UsageRecord], dev: ScoredRun | None, volume: int | None
) -> UnitEconomics:
    """Per-query **production** economics: the AGENT's cost only, from the dev run's agent-role
    ledger rows (so it reconciles with ``usage.db`` exactly). Layer-2 judge spend is a dev-only
    cost — it doesn't run in production — so it's reported separately (``judge_cost_per_query``)
    and never folded into ``$/query`` (invariant 5). Zeroed when there's no measured dev run."""
    if dev is None or dev.total == 0:
        return UnitEconomics(
            cost_per_query_usd=0.0,
            tokens_per_query=0.0,
            cache_hit_rate=0.0,
            volume_per_month=volume,
            projected_monthly_usd=None,
        )
    run_rows = [r for r in rows if r.run_id == dev.run_id]
    agent_rows = [r for r in run_rows if r.role == "agent"]
    judge_rows = [r for r in run_rows if r.role == "judge"]

    cost = sum(r.cost_usd or 0.0 for r in agent_rows)
    input_tokens = sum(r.input_tokens for r in agent_rows)
    cache_reads = sum(r.cache_read_input_tokens for r in agent_rows)
    tokens = sum(
        r.input_tokens + r.output_tokens + r.cache_creation_input_tokens + r.cache_read_input_tokens
        for r in agent_rows
    )
    cache_denom = input_tokens + cache_reads
    cost_per_query = cost / dev.total
    return UnitEconomics(
        cost_per_query_usd=cost_per_query,
        tokens_per_query=tokens / dev.total,
        cache_hit_rate=(cache_reads / cache_denom) if cache_denom else 0.0,
        judge_cost_per_query_usd=sum(r.cost_usd or 0.0 for r in judge_rows) / dev.total,
        volume_per_month=volume,
        projected_monthly_usd=cost_per_query * volume if volume is not None else None,
    )


def _quality(run: ScoredRun) -> QualityKpis:
    low, high = run.accuracy_ci()
    return QualityKpis(
        split=run.split,
        n=run.total,
        accuracy=run.accuracy,
        accuracy_low=low,
        accuracy_high=high,
        manual_review_rate=run.manual_review_rate,
        self_repair_rate=run.self_repair_rate,
        low_confidence=run.total < SMALL_N_FLOOR,
        models=dict(run.models),  # which model scored THIS split — so dev/test can't be conflated
    )


def _performance(run: ScoredRun) -> PerformanceKpis:
    latencies = [r.latency_ms for r in run.results]
    return PerformanceKpis(
        n=run.total,
        latency_p50_ms=percentile(latencies, 50),
        latency_p95_ms=percentile(latencies, 95),
        low_confidence=run.total < SMALL_N_FLOOR,
    )


def build_report(project: Project, *, volume: int | None = None) -> KpiReport:
    """Roll ``.sqbyl/`` into a :class:`KpiReport` (spec §7.5). No tokens, no DB query."""
    from sqbyl.eval.report import load_runs

    paths = SqbylPaths(project.root)
    rows = _load_usage(paths)
    dev_runs = load_runs(paths, split="dev")
    dev = dev_runs[-1] if dev_runs else None
    test_runs = load_runs(paths, split="test")
    test = test_runs[-1] if test_runs else None

    target = project.manifest.defaults.readiness_target
    total_tokens = sum(
        r.input_tokens + r.output_tokens + r.cache_creation_input_tokens + r.cache_read_input_tokens
        for r in rows
    )
    return KpiReport(
        models=dict(dev.models) if dev is not None else {},
        unit_economics=_unit_economics(rows, dev, volume),
        dev_quality=_quality(dev) if dev is not None else None,
        test_quality=_quality(test) if test is not None else None,
        dev_test_gap=(dev.accuracy - test.accuracy) if (dev and test) else None,
        performance=_performance(dev) if dev is not None else None,
        readiness_target=target,
        readiness_gap=(target - dev.accuracy) if dev is not None else None,
        round_trips_to_ship=len(dev_runs),
        total_cost_usd=sum(r.cost_usd or 0.0 for r in rows),
        total_tokens=total_tokens,
    )
