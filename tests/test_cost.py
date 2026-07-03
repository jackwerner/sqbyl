"""Cost machinery — pricing, estimation, the live spend meter, and the budget cap (§9).

The pricing/estimate stubs came first (invariant 6: paid commands route through an
estimator from day one). Phase 7.1 adds the structured :class:`CostEstimate`, the
:class:`SpendMeter`, and ``--dry-run`` / ``sqbyl cost``. The plan's "done when":
``--dry-run`` produces an estimate with **zero** API calls; a budget cap **provably
halts** a run; usage rows **reconcile** with the meter.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sqbyl.cli import main
from sqbyl_runtime.cost import (
    BudgetError,
    CostEstimate,
    EstimateItem,
    SpendMeter,
    estimate_cost,
    price_usage,
    rate_for,
)
from sqbyl_runtime.llm.base import Usage
from sqbyl_runtime.state.layout import SqbylPaths
from sqbyl_runtime.state.usage import UsageStore


@pytest.fixture
def project(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused-in-dry-run")
    dst = tmp_path / "proj"
    shutil.copytree(dogfood_dir, dst)
    return dst


def _usage_rows(project: Path) -> list[object]:
    db = SqbylPaths(project).usage_db
    if not db.exists():
        return []
    with UsageStore(db) as store:
        return list(store.all())


def test_price_usage_includes_cache_tokens() -> None:
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_input_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
    )
    rate = rate_for("claude-opus-4-8")
    expected = rate.input + rate.output + rate.cache_write + rate.cache_read
    assert price_usage(usage, "claude-opus-4-8") == expected


def test_unknown_model_falls_back_to_flagship_not_zero() -> None:
    usage = Usage(input_tokens=1_000_000)
    assert price_usage(usage, "some-future-model") == rate_for("claude-opus-4-8").input
    assert price_usage(usage, "some-future-model") > 0


def test_estimate_scales_with_calls() -> None:
    one = estimate_cost(
        model="claude-opus-4-8", calls=1, avg_input_tokens=1500, avg_output_tokens=400
    )
    ten = estimate_cost(
        model="claude-opus-4-8", calls=10, avg_input_tokens=1500, avg_output_tokens=400
    )
    assert one > 0
    assert ten == one * 10


def test_cheaper_model_estimates_less() -> None:
    kw = {"calls": 5, "avg_input_tokens": 1500, "avg_output_tokens": 400}
    assert estimate_cost(model="claude-haiku-4-5-20251001", **kw) < estimate_cost(
        model="claude-opus-4-8", **kw
    )


# ── the structured estimate ─────────────────────────────────────────────────────────────


def test_estimate_itemizes_and_totals() -> None:
    est = CostEstimate(
        items=[
            EstimateItem(
                label="annotate 2 tables",
                model="claude-opus-4-8",
                calls=2,
                avg_input_tokens=1500,
                avg_output_tokens=400,
            ),
            EstimateItem(
                label="synth",
                model="claude-haiku-4-5-20251001",
                calls=1,
                avg_input_tokens=2000,
                avg_output_tokens=2000,
            ),
        ]
    )
    # opus: 2 × (1500·15 + 400·75)/1e6 = 0.105 ; haiku: (2000·1 + 2000·5)/1e6 = 0.012
    assert est.items[0].cost_usd == pytest.approx(0.105)
    assert est.total_usd == pytest.approx(0.117)
    assert est.calls == 3
    rendered = est.render()
    assert "annotate 2 tables" in rendered and "estimated total" in rendered


def test_empty_estimate_is_zero() -> None:
    est = CostEstimate()
    assert est.total_usd == 0.0
    assert est.calls == 0
    assert "no paid work" in est.render()


def test_estimate_ceilings_fold_in_self_repair_and_the_judge_panel() -> None:
    # The estimate must never under-read: the agent line assumes every question may exhaust
    # its self-repair retries, and the judge line assumes the whole panel per review row.
    from sqbyl.estimates import _JUDGE_PANEL, ask_estimate, eval_estimate

    # ask: 1 primary + 2 repairs = 3 agent calls.
    assert ask_estimate("claude-opus-4-8", self_repair_attempts=2).calls == 3

    # eval: 5 questions × (1+2) agent calls + 5 × panel judge calls.
    est = eval_estimate(
        "claude-opus-4-8",
        questions=5,
        judge_model="claude-opus-4-8",
        self_repair_attempts=2,
    )
    assert est.items[0].calls == 5 * 3  # agent, with repair headroom
    assert est.items[1].calls == 5 * _JUDGE_PANEL  # the full judge panel, not 1×
    assert _JUDGE_PANEL >= 3

    # With judging off, only the agent line is budgeted.
    assert len(eval_estimate("claude-opus-4-8", questions=5).items) == 1


# ── the spend meter: accounting + reconciliation ────────────────────────────────────────


def test_meter_reconciles_with_the_usage_ledger(tmp_path: Path) -> None:
    with UsageStore(tmp_path / "usage.db") as store:
        meter = SpendMeter(store=store, command="annotate")
        usages = [Usage(input_tokens=1000, output_tokens=200), Usage(input_tokens=500)]
        for u in usages:
            meter.record(u, model="claude-opus-4-8", role="annotator", run_id="r1")

        expected = sum(price_usage(u, "claude-opus-4-8") for u in usages)
        assert meter.spent == pytest.approx(expected)
        ledger = store.all()
        assert len(ledger) == 2
        assert sum(row.cost_usd or 0.0 for row in ledger) == pytest.approx(meter.spent)
        assert all(row.command == "annotate" and row.role == "annotator" for row in ledger)


def test_meter_without_a_store_still_tallies() -> None:
    meter = SpendMeter(budget=1.0)
    cost = meter.record(Usage(input_tokens=1000), model="claude-opus-4-8")
    assert cost == pytest.approx(price_usage(Usage(input_tokens=1000), "claude-opus-4-8"))
    assert meter.spent == cost


# ── the budget cap: would_exceed / guard / halt ─────────────────────────────────────────


def test_uncapped_meter_never_exceeds() -> None:
    meter = SpendMeter()
    assert meter.remaining is None
    assert meter.would_exceed(1_000_000.0) is False
    meter.guard(1_000_000.0)  # does not raise


def test_guard_halts_when_the_next_step_wont_fit() -> None:
    meter = SpendMeter(budget=0.10)
    meter.record(Usage(input_tokens=1500, output_tokens=400), model="claude-opus-4-8")  # ~$0.0525
    assert meter.remaining == pytest.approx(0.0475)
    assert meter.would_exceed(0.0525) is True

    with pytest.raises(BudgetError) as exc:
        meter.guard(0.0525)
    assert exc.value.budget == 0.10
    assert exc.value.spent == pytest.approx(0.0525)
    assert exc.value.attempted == pytest.approx(0.0525)


def test_estimate_exactly_at_budget_is_allowed() -> None:
    meter = SpendMeter(budget=0.0525)
    assert meter.would_exceed(0.0525) is False
    meter.guard(0.0525)  # no raise


# ── dry-run / `sqbyl cost`: an estimate for zero API calls ───────────────────────────────


def test_cost_command_estimates_without_spending(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*a: object, **k: object) -> object:
        raise AssertionError("cost/dry-run must not build an LLM client")

    monkeypatch.setattr("sqbyl.llm.build_llm_client", _boom)

    code = main(["cost", "annotate", str(project)])
    assert code == 0
    out = capsys.readouterr().out
    assert "no API calls" in out and "estimated total" in out
    assert _usage_rows(project) == []


def test_dry_run_flag_spends_nothing_on_every_paid_command(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "sqbyl.llm.build_llm_client",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no client on dry run")),
    )
    for command in ("annotate", "synth", "eval", "coach", "ask"):
        argv = [command, "hi", str(project)] if command == "ask" else [command, str(project)]
        code = main([*argv, "--dry-run"])
        assert code == 0, command
        assert _usage_rows(project) == []


def test_cost_rejects_a_free_command(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["cost", "introspect", str(project)])
    assert code == 2
    assert "not a paid command" in capsys.readouterr().out


def test_auto_without_budget_is_refused(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["synth", str(project), "--auto"])
    assert code == 2
    assert "--auto requires --budget" in capsys.readouterr().out


def test_ask_honors_the_uniform_budget_gate(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # `ask` is a paid command, so it too accepts --budget/--auto and hard-stops under --auto.
    assert main(["ask", "hi", str(project), "--auto"]) == 2  # --auto needs --budget
    assert "--auto requires --budget" in capsys.readouterr().out

    code = main(["ask", "hi", str(project), "--auto", "--budget", "0.0001"])
    assert code == 1  # estimate exceeds the cap → hard stop, nothing spent
    assert "exceeds budget" in capsys.readouterr().out
    assert _usage_rows(project) == []


def test_guided_over_budget_pauses_and_asks(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Guided (no --auto): a budget under one table's estimate pauses at the first table.
    # Answering "n" declines → no table runs, nothing metered (the pause-and-ask of spec §5.5).
    # A mock whose .complete would raise if reached proves no paid call was made.
    from sqbyl_runtime.llm.mock import MockLLMClient

    monkeypatch.setattr("sqbyl.llm.build_llm_client", lambda *a, **k: MockLLMClient([]))
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    code = main(["annotate", str(project), "--budget", "0.0001"])
    assert code == 0  # a clean, chosen stop
    assert "annotated 0/" in capsys.readouterr().out
    assert _usage_rows(project) == []
