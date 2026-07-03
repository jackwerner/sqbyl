"""Phase 7.2 — the guided `sqbyl init` push (spec §5.5).

The plan's "done when": the full journey-doc flow runs against the fixture under
record-replay (here a scripted mock, zero tokens — invariant 4); `--auto` without
`--budget` errors; an unchanged re-run does no paid work. Plus the free-pass / plan /
incremental-skip logic that decides *what* to (re-)orchestrate.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sqbyl import init as initmod
from sqbyl.cli import main
from sqbyl.orchestrator import Orchestrator
from sqbyl.project import Project
from sqbyl_runtime.cost import SpendMeter
from sqbyl_runtime.llm.mock import MockLLMClient, structured_reply
from sqbyl_runtime.state.layout import SqbylPaths
from sqbyl_runtime.state.usage import UsageStore


@pytest.fixture
def cold(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A cold-start project: manifest + judges, but no semantics and no dev set yet."""
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused")
    dst = tmp_path / "cold"
    shutil.copytree(dogfood_dir, dst)
    for stale in (dst / "semantics").glob("*.yaml"):
        stale.unlink()
    (dst / "benchmarks" / "dev.yaml").unlink()
    return dst


def _usage_rows(project: Path) -> list[object]:
    db = SqbylPaths(project).usage_db
    if not db.exists():
        return []
    with UsageStore(db) as store:
        return list(store.all())


# ── the free pass ($0) ──────────────────────────────────────────────────────────────────


def test_free_pass_profiles_every_table_and_counts_joins(cold: Path) -> None:
    project = Project.load(cold)
    free = initmod.run_free_pass(project)
    assert free.n_tables == 2  # analytics.orders + analytics.customers
    assert free.n_columns > 0
    assert free.joins >= 1  # the orders→customers FK
    # Drafts were written and profiled, no LLM spent.
    assert sorted(p.name for p in project.semantics_dir.glob("*.yaml")) == [
        "customers.yaml",
        "orders.yaml",
    ]
    assert _usage_rows(cold) == []


# ── the costed plan: only un-done work ──────────────────────────────────────────────────


def test_plan_prices_annotate_synth_eval_on_a_cold_project(cold: Path) -> None:
    project = Project.load(cold)
    free = initmod.run_free_pass(project)
    plan = initmod.build_plan(project, free, model="claude-opus-4-8", synth_n=10)
    assert plan.annotate_tables == ["customers.yaml", "orders.yaml"]
    assert plan.do_synth is True
    assert plan.do_eval is True
    assert plan.estimate.total_usd > 0
    # Three stages priced.
    labels = " ".join(i.label for i in plan.estimate.items)
    assert "annotate" in labels and "synth" in labels and "eval" in labels


def test_after_a_full_run_the_plan_is_empty(cold: Path) -> None:
    # Run the whole journey once, then re-plan: nothing changed → no paid work (idempotent).
    project = Project.load(cold)
    free = initmod.run_free_pass(project)
    plan = initmod.build_plan(project, free, model="claude-opus-4-8", synth_n=2)
    paths = SqbylPaths(project.root).ensure()
    with UsageStore(paths.usage_db) as store:
        meter = SpendMeter(store=store, command="init")
        initmod.enrich(
            project,
            plan,
            llm=_journey_client(),
            meter=meter,
            orchestrator=Orchestrator(concurrency=1),
            authorize=lambda *_: True,
            schema_fingerprint=free.schema_fingerprint,
        )

    free2 = initmod.run_free_pass(project)  # re-profiling is byte-idempotent
    replan = initmod.build_plan(project, free2, model="claude-opus-4-8", synth_n=2)
    assert replan.annotate_tables == []  # tables unchanged since annotation
    assert replan.do_synth is False  # dev set now exists
    assert replan.do_eval is False  # baseline already ran at this content hash
    assert replan.has_paid_work is False


def test_tables_needing_annotation_tracks_changes(cold: Path) -> None:
    from sqbyl.semantics_io import dump_yaml_path
    from sqbyl.yamlio import load_yaml

    project = Project.load(cold)
    initmod.run_free_pass(project)
    # Fresh drafts have no description → all need annotation.
    assert {p.name for p in initmod.tables_needing_annotation(project)} == {
        "customers.yaml",
        "orders.yaml",
    }
    # Give one a description and stamp it → it drops out; the other still needs work.
    orders = project.semantics_dir / "orders.yaml"
    raw = load_yaml(orders.read_text())
    raw["description"] = "One row per order."
    dump_yaml_path(raw, orders)
    initmod._record_annotated(project, orders)
    assert {p.name for p in initmod.tables_needing_annotation(project)} == {"customers.yaml"}


# ── the guard rails: --auto needs --budget, --dry-run spends nothing ─────────────────────


def test_init_auto_without_budget_errors(cold: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["init", str(cold), "--auto"]) == 2
    assert "--auto requires --budget" in capsys.readouterr().out


def test_init_dry_run_shows_plan_and_spends_nothing(
    cold: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "sqbyl.llm.build_llm_client",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry run must not build a client")),
    )
    code = main(["init", str(cold), "--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert "free pass" in out and "estimated total" in out and "dry run" in out
    assert _usage_rows(cold) == []


def test_unchanged_rerun_does_no_paid_work(
    cold: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The done-when: after a full init, re-running does zero paid work — never even builds a
    # client. Run the journey once, then re-run via the CLI and assert nothing more is spent.
    project = Project.load(cold)
    free = initmod.run_free_pass(project)
    plan = initmod.build_plan(project, free, model="claude-opus-4-8", synth_n=2)
    paths = SqbylPaths(project.root).ensure()
    with UsageStore(paths.usage_db) as store:
        meter = SpendMeter(store=store, command="init")
        initmod.enrich(
            project,
            plan,
            llm=_journey_client(),
            meter=meter,
            orchestrator=Orchestrator(concurrency=1),
            authorize=lambda *_: True,
            schema_fingerprint=free.schema_fingerprint,
        )
    rows_before = len(_usage_rows(cold))

    monkeypatch.setattr(
        "sqbyl.llm.build_llm_client",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("nothing changed — no client")),
    )
    code = main(["init", str(cold)])
    assert code == 0
    assert "already up to date" in capsys.readouterr().out
    assert len(_usage_rows(cold)) == rows_before  # not a single new metered call


# ── the full journey (scripted mock, zero tokens) ───────────────────────────────────────


def _journey_client() -> MockLLMClient:
    """Scripts one full init: 2 annotate calls, 1 synth draft, 2 baseline-eval agent calls.

    The two synth questions echo gold SQL that runs on the fixture, and the agent returns the
    same SQL, so the baseline eval scores both correct — no judge/coach calls fire, keeping
    the cursor-ordered mock deterministic.
    """
    annotate = structured_reply(
        {"description": "A table.", "synonyms": [], "confidence": 0.9, "columns": []}
    )
    q1 = "SELECT COUNT(*) FROM analytics.orders"
    q2 = "SELECT SUM(amount_cents) FROM analytics.orders"
    synth = structured_reply(
        {
            "questions": [
                {"question": "How many orders are there?", "gold_sql": q1},
                {"question": "What is total revenue in cents?", "gold_sql": q2},
            ]
        }
    )
    agent1 = structured_reply({"plan": "count orders", "sql": q1})
    agent2 = structured_reply({"plan": "sum revenue", "sql": q2})
    return MockLLMClient([annotate, annotate, synth, agent1, agent2])


def test_enrich_runs_the_full_journey_to_a_queue(cold: Path) -> None:
    project = Project.load(cold)
    free = initmod.run_free_pass(project)
    plan = initmod.build_plan(project, free, model="claude-opus-4-8", synth_n=2)

    paths = SqbylPaths(project.root).ensure()
    with UsageStore(paths.usage_db) as store:
        meter = SpendMeter(store=store, command="init")
        result = initmod.enrich(
            project,
            plan,
            llm=_journey_client(),
            meter=meter,
            orchestrator=Orchestrator(concurrency=1),  # cursor-mock is not thread-safe
            authorize=lambda *_: True,
            schema_fingerprint=free.schema_fingerprint,
        )

    # Every stage ran: 2 tables annotated, 2 execution-grounded questions in the dev set,
    # a baseline eval scored, and a leverage-sorted queue assembled.
    assert result.annotated == 2
    assert result.survivors == 2
    assert result.run is not None and result.run.total == 2
    assert result.run.accuracy == pytest.approx(1.0)  # agent echoed gold → both correct
    assert result.queue is not None
    assert result.spent_usd > 0
    # The survivors landed in dev (never test — invariant 3).
    assert (project.root / "benchmarks" / "dev.yaml").exists()
    assert not (project.root / "benchmarks" / "test.yaml").read_text().count("How many orders")
    # Spend was metered to the ledger and reconciles with the meter.
    ledger = _usage_rows(cold)
    assert sum(getattr(r, "cost_usd", 0.0) or 0.0 for r in ledger) == pytest.approx(
        result.spent_usd
    )
    # Every paid stage wrote an OTel-GenAI trace, not just eval (invariant 7).
    traces = SqbylPaths(project.root).traces_dir
    assert (traces / "annotate.jsonl").exists()
    assert (traces / "synth.jsonl").exists()
    assert (traces / "eval.jsonl").exists()


def test_baseline_skip_respects_schema_and_as_of(cold: Path) -> None:
    # The eval-skip gate must key on the live schema + --as-of, not just the file content hash,
    # so a changed DB or a different clock forces a re-eval (ml-systems: stale-baseline risk).
    project = Project.load(cold)
    free = initmod.run_free_pass(project)
    plan = initmod.build_plan(project, free, model="claude-opus-4-8", synth_n=2)
    paths = SqbylPaths(project.root).ensure()
    with UsageStore(paths.usage_db) as store:
        meter = SpendMeter(store=store, command="init")
        initmod.enrich(
            project,
            plan,
            llm=_journey_client(),
            meter=meter,
            orchestrator=Orchestrator(concurrency=1),
            authorize=lambda *_: True,
            schema_fingerprint=free.schema_fingerprint,
        )

    free2 = initmod.run_free_pass(project)
    # Same schema + same (None) as_of → baseline is current, eval skipped.
    assert initmod.build_plan(project, free2, model="claude-opus-4-8", synth_n=2).do_eval is False
    # A drifted schema fingerprint → not current → re-eval.
    import dataclasses

    drifted = dataclasses.replace(free2, schema_fingerprint="sha256:different")
    assert initmod.build_plan(project, drifted, model="claude-opus-4-8", synth_n=2).do_eval is True
    # A different --as-of → not current → re-eval (time-relative gold must be re-grounded).
    from datetime import datetime

    other = datetime(2020, 1, 1)
    assert (
        initmod.build_plan(project, free2, model="claude-opus-4-8", synth_n=2, as_of=other).do_eval
        is True
    )


def test_enrich_stops_cleanly_when_a_stage_is_declined(cold: Path) -> None:
    # authorize returns False on the first stage → nothing runs, nothing metered.
    project = Project.load(cold)
    free = initmod.run_free_pass(project)
    plan = initmod.build_plan(project, free, model="claude-opus-4-8", synth_n=2)
    paths = SqbylPaths(project.root).ensure()
    with UsageStore(paths.usage_db) as store:
        meter = SpendMeter(budget=0.0001, store=store, command="init")
        result = initmod.enrich(
            project,
            plan,
            llm=_journey_client(),
            meter=meter,
            orchestrator=Orchestrator(concurrency=1),
            authorize=lambda *_: False,
        )
    assert result.stopped is True
    assert result.annotated == 0
    assert result.spent_usd == 0.0
