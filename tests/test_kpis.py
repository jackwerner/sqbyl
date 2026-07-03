"""Phase 7.3 — operational KPIs + `sqbyl report` (spec §7.5).

The plan's "done when": `sqbyl report` against a project (after an eval run) emits a
`KpiReport` that validates against its model and reconciles $/query with `usage.db`;
`--json` round-trips; no tokens are spent. Plus: dev and held-out test are reported
separately (never conflated), and only aggregates — never row data (§13) — are emitted.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from sqbyl.cli import main
from sqbyl.eval.report import save_run
from sqbyl.models import KpiReport, QuestionResult, ScoredRun, Verdict
from sqbyl.project import Project
from sqbyl_runtime.cost import price_usage
from sqbyl_runtime.llm.base import Usage
from sqbyl_runtime.state.layout import SqbylPaths
from sqbyl_runtime.state.usage import UsageRecord, UsageStore

_MODEL = "claude-opus-4-8"


def _q(
    qid: str,
    verdict: Verdict,
    *,
    tokens: int,
    latency: float,
    repaired: bool = False,
    judge_tokens: int = 0,
) -> QuestionResult:
    usage = Usage(input_tokens=tokens, output_tokens=tokens // 5, cache_read_input_tokens=tokens)
    judge_usage = Usage(input_tokens=judge_tokens) if judge_tokens else Usage()
    return QuestionResult(
        id=qid,
        question=f"question {qid}?",
        generated_sql=f"SELECT '{qid}'",
        gold_sql=f"SELECT '{qid}'",
        verdict=verdict,
        usage=usage,
        cost_usd=price_usage(usage, _MODEL),
        judge_usage=judge_usage,
        judge_cost_usd=price_usage(judge_usage, _MODEL),
        latency_ms=latency,
        attempts=2 if repaired else 1,
        repaired=repaired,
    )


def _persist_run(paths: SqbylPaths, run: ScoredRun) -> None:
    """Save the run and mirror its per-question spend into usage.db exactly as ``project.eval``
    does — an ``agent`` row and, when the row was judged, a separate ``judge`` row under the
    same run_id — so the report's agent-only reconciliation is tested against a judged run."""
    save_run(paths, run)
    with UsageStore(paths.usage_db) as store:
        for r in run.results:
            store.record(
                UsageRecord.from_usage(
                    r.usage,
                    model=_MODEL,
                    command="eval",
                    role="agent",
                    cost_usd=r.cost_usd,
                    run_id=run.run_id,
                )
            )
            if r.judge_usage.total_tokens:
                store.record(
                    UsageRecord.from_usage(
                        r.judge_usage,
                        model=_MODEL,
                        command="eval",
                        role="judge",
                        cost_usd=r.judge_cost_usd,
                        run_id=run.run_id,
                    )
                )


@pytest.fixture
def project(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Project:
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused")
    dst = tmp_path / "proj"
    shutil.copytree(dogfood_dir, dst)
    project = Project.load(dst)
    paths = SqbylPaths(dst).ensure()

    # A dev run: 4 questions, 3 correct, 1 review; one needed a repair; varied latency.
    dev = ScoredRun(
        run_id="dev_run",
        split="dev",
        models={"agent": _MODEL, "judge": _MODEL},
        results=[
            _q("a", Verdict.correct, tokens=1000, latency=100.0),
            _q("b", Verdict.correct, tokens=1000, latency=200.0, repaired=True),
            _q("c", Verdict.correct, tokens=1000, latency=300.0),
            # The review-pile row was judged — a judge-role row lands in usage.db beside it.
            _q("d", Verdict.manual_review, tokens=1000, latency=400.0, judge_tokens=500),
        ],
    )
    _persist_run(paths, dev)
    # A held-out test run: 2 questions, 1 correct (lower than dev → a dev↔test gap).
    test = ScoredRun(
        run_id="test_run",
        split="test",
        models={"agent": _MODEL},
        results=[
            _q("t1", Verdict.correct, tokens=1000, latency=150.0),
            _q("t2", Verdict.incorrect, tokens=1000, latency=150.0),
        ],
    )
    _persist_run(paths, test)
    return project


# ── the rollup ──────────────────────────────────────────────────────────────────────────


def test_report_validates_and_separates_dev_from_test(project: Project) -> None:
    report = project.kpis()
    assert isinstance(report, KpiReport)

    # Dev and held-out test are separate — never one conflated accuracy (spec §7).
    assert report.dev_quality is not None and report.dev_quality.split == "dev"
    assert report.dev_quality.accuracy == pytest.approx(0.75)  # 3/4
    assert report.dev_quality.self_repair_rate == pytest.approx(0.25)  # 1/4 repaired
    assert report.dev_quality.manual_review_rate == pytest.approx(0.25)
    assert report.test_quality is not None and report.test_quality.split == "test"
    assert report.test_quality.accuracy == pytest.approx(0.5)  # 1/2
    # The overfitting signal is first-class.
    assert report.dev_test_gap == pytest.approx(0.25)  # 0.75 − 0.50
    # Small eval sets are flagged, not dressed up as precise.
    assert report.dev_quality.low_confidence is True


def test_cost_per_query_is_agent_only_and_reconciles_with_the_ledger(project: Project) -> None:
    report = project.kpis()
    ue = report.unit_economics
    n = report.dev_quality.n  # type: ignore[union-attr]
    paths = SqbylPaths(project.root)
    with UsageStore(paths.usage_db) as store:
        rows = [r for r in store.all() if r.run_id == "dev_run"]
    agent_cost = sum(r.cost_usd or 0.0 for r in rows if r.role == "agent")
    judge_cost = sum(r.cost_usd or 0.0 for r in rows if r.role == "judge")
    assert judge_cost > 0  # the fixture is a judged run (the default config)

    # $/query is the AGENT's production cost — it reconciles with the agent-role ledger rows
    # and pointedly EXCLUDES the dev-only judge spend (the ml-systems fix).
    assert ue.cost_per_query_usd * n == pytest.approx(agent_cost)
    assert ue.cost_per_query_usd * n < (agent_cost + judge_cost)  # judge not folded in
    # Judge overhead is reported separately, and it too reconciles with the ledger.
    assert ue.judge_cost_per_query_usd * n == pytest.approx(judge_cost)
    # Cache-hit rate is the agent's (each question read 1000 cached + 1000 fresh → 50%).
    assert ue.cache_hit_rate == pytest.approx(0.5)


def test_projected_run_rate_scales_with_volume(project: Project) -> None:
    report = project.kpis(volume=10_000)
    ue = report.unit_economics
    assert ue.volume_per_month == 10_000
    assert ue.projected_monthly_usd == pytest.approx(ue.cost_per_query_usd * 10_000)


def test_performance_percentiles_come_from_the_run(project: Project) -> None:
    p = project.kpis().performance
    assert p is not None
    assert p.latency_p50_ms == pytest.approx(250.0)  # median of 100/200/300/400
    assert p.latency_p95_ms >= p.latency_p50_ms


def test_readiness_and_round_trips(project: Project) -> None:
    report = project.kpis()
    # Default target 0.95; dev accuracy 0.75 → 0.20 to go, not yet reached.
    assert report.readiness_gap == pytest.approx(0.20)
    assert report.readiness_met is False
    assert report.round_trips_to_ship == 1  # one dev run recorded


def test_quality_carries_per_split_model_provenance(project: Project) -> None:
    # Each split records which agent scored it, so the dev↔test gap can't silently conflate a
    # model change with overfitting (ml-systems).
    report = project.kpis()
    assert report.dev_quality is not None and report.dev_quality.models.get("agent") == _MODEL
    assert report.test_quality is not None and report.test_quality.models.get("agent") == _MODEL


def test_performance_flags_small_samples(project: Project) -> None:
    # n=4 is far below the small-sample floor, so p95 is directional, not a settled tail.
    perf = project.kpis().performance
    assert perf is not None and perf.low_confidence is True


def test_readiness_requires_the_ci_to_clear_target_not_just_the_point(project: Project) -> None:
    from datetime import UTC, datetime

    paths = SqbylPaths(project.root)
    # A tiny *perfect* run: point estimate 100% ≥ 95% target, but the interval is far too wide.
    # CI-gated readiness must NOT read this as shipped (the whole point of the fix).
    small_perfect = ScoredRun(
        run_id="dev_small_perfect",
        split="dev",
        created_at=datetime(2027, 1, 1, tzinfo=UTC),  # newest dev run
        models={"agent": _MODEL},
        results=[_q(f"s{i}", Verdict.correct, tokens=100, latency=10.0) for i in range(4)],
    )
    save_run(paths, small_perfect)
    r = project.kpis()
    assert r.dev_quality is not None
    assert r.dev_quality.accuracy == pytest.approx(1.0)
    assert r.dev_quality.accuracy_low < 0.95  # the interval straddles the target
    assert r.readiness_met is False  # point says met, CI says no → not shipped

    # A large run whose lower CI bound actually clears the target → shipped.
    big = ScoredRun(
        run_id="dev_big",
        split="dev",
        created_at=datetime(2027, 1, 2, tzinfo=UTC),  # now the newest
        models={"agent": _MODEL},
        results=[_q(f"b{i}", Verdict.correct, tokens=100, latency=10.0) for i in range(200)],
    )
    save_run(paths, big)
    r2 = project.kpis()
    assert r2.dev_quality is not None and r2.dev_quality.accuracy_low >= 0.95
    assert r2.readiness_met is True


# ── the CLI: --json round-trips, aggregates only, $0 ────────────────────────────────────


def test_report_cli_json_round_trips_and_spends_nothing(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    before = len(_all_usage(project))
    code = main(["report", str(project.root), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    # Round-trips back into the model.
    reloaded = KpiReport.model_validate(payload)
    assert reloaded.dev_quality is not None
    # The report is a pure view — it wrote no new usage rows (spends nothing, §7.5).
    assert len(_all_usage(project)) == before


def test_report_emits_aggregates_never_row_data(project: Project) -> None:
    # §13: the report must expose numbers, never question text / SQL / result rows.
    blob = project.kpis().model_dump_json()
    assert "SELECT" not in blob
    assert "question a" not in blob and "gold_sql" not in blob


def test_report_human_table_shows_dev_and_test(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["report", str(project.root)]) == 0
    out = capsys.readouterr().out
    assert "unit economics" in out and "$/query" in out
    assert "quality — dev" in out and "quality — test" in out
    assert "dev↔test gap" in out


def _all_usage(project: Project) -> list[object]:
    with UsageStore(SqbylPaths(project.root).usage_db) as store:
        return list(store.all())
