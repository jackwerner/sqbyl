"""Phase 8.3 — the autonomous Optimizer (`sqbyl optimize`, spec §6.C).

The plan's "done when": on a fixable broken project, optimize reaches the target within budget
under a scripted (record-replay-style) client; it **provably never reads test.yaml** during the
loop; and it returns a frontier for selection with the held-out test scored **once**.

The loop's causality (an edit makes the agent better) is *simulated* by a cursor-ordered
`MockLLMClient`: the baseline answer is scripted wrong, and the post-coach answer scripted
right. That's the honest way to unit-test loop *mechanics* — keep-if-helped, revert-if-not,
the frontier, the budget hard-stop, and the single held-out scoring — without a live model.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sqbyl.cli import main
from sqbyl.models.optimize import StopReason
from sqbyl.optimize import optimize
from sqbyl.project import Project
from sqbyl_runtime.llm.base import Usage
from sqbyl_runtime.llm.mock import MockLLMClient, structured_reply

# Dev gold counts orders; test gold counts customers — two different, executable answers.
_DEV = "- id: d1\n  question: How many orders?\n  gold_sql: SELECT COUNT(*) FROM analytics.orders\n"
_TEST = (
    "- id: t1\n  question: How many customers?\n"
    "  gold_sql: SELECT COUNT(*) FROM analytics.customers\n"
)
_ORDERS = "SELECT COUNT(*) FROM analytics.orders"  # correct for dev
_CUSTOMERS = "SELECT COUNT(*) FROM analytics.customers"  # correct for test, WRONG for dev


def _agent(sql: str) -> object:
    return structured_reply(
        {"plan": "p", "sql": sql, "used_assets": []},
        usage=Usage(input_tokens=200, output_tokens=20),
    )


def _coach_proposal() -> object:
    # Appends a valid Example (so the next eval's load_knowledge parses it) to a new file.
    example = "- question: How many orders?\n  sql: SELECT COUNT(*) FROM analytics.orders\n"
    return structured_reply(
        {
            "proposals": [
                {
                    "title": "teach the orders count",
                    "root_cause": "agent counted the wrong table",
                    "layer": "example",
                    "target_file": "examples/learned.yaml",
                    "edits": [{"find": "", "replace": example}],
                    "predicted_fixes": 1,
                    "confidence": 0.9,
                    "question_ids": ["d1"],
                }
            ]
        },
        usage=Usage(input_tokens=500, output_tokens=80),
    )


@pytest.fixture
def broken(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Project:
    """The dogfood project pared to one dev + one held-out question, judging off — a tiny,
    fully-controlled loop whose every LLM call is scriptable in order."""
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused")
    dst = tmp_path / "proj"
    shutil.copytree(dogfood_dir, dst)
    (dst / "benchmarks" / "dev.yaml").write_text(_DEV)
    (dst / "benchmarks" / "test.yaml").write_text(_TEST)
    manifest = (dst / "sqbyl.yaml").read_text().replace("auto_judge: true", "auto_judge: false")
    (dst / "sqbyl.yaml").write_text(manifest)
    return Project.load(dst)


# ── the happy path: reach target within budget, frontier + one held-out score ─────────────


def test_optimize_reaches_target_within_budget(broken: Project) -> None:
    # baseline dev (wrong) → coach → trial dev (right) → target met → held-out scored once.
    llm = MockLLMClient(
        [_agent(_CUSTOMERS), _coach_proposal(), _agent(_ORDERS), _agent(_CUSTOMERS)]
    )
    result = optimize(broken, llm=llm, target=0.9, budget=10.0)

    assert result.stopped is StopReason.target_met
    assert result.rounds == 1
    # Frontier: baseline (0%) then the accepted edit (100%).
    assert [p.dev_accuracy for p in result.frontier] == pytest.approx([0.0, 1.0])
    assert result.picked == 1
    assert result.picked_point.proposal_title == "teach the orders count"
    assert result.picked_point.layer == "example"
    assert result.improved == pytest.approx(1.0)
    # Held-out scored ONCE, on the picked version.
    assert result.test_accuracy == pytest.approx(1.0)
    assert result.test_n == 1
    assert result.dev_test_gap == pytest.approx(0.0)
    # The accepted edit is a real working-tree change the user can `git diff`.
    assert (broken.root / "examples" / "learned.yaml").exists()
    assert result.spent_usd > 0


def test_optimize_scores_the_heldout_set_exactly_once_and_after_the_loop(
    broken: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Provenance for "never optimizes on test": record every split the loop evaluates. The
    # test split must appear exactly once, and only as the final call (never fed to the Coach).
    splits: list[str] = []
    real_eval = Project.eval

    def _spy(self: Project, split: str = "dev", **kw: object) -> object:
        splits.append(split)
        return real_eval(self, split, **kw)

    monkeypatch.setattr(Project, "eval", _spy)
    llm = MockLLMClient(
        [_agent(_CUSTOMERS), _coach_proposal(), _agent(_ORDERS), _agent(_CUSTOMERS)]
    )
    optimize(broken, llm=llm, target=0.9, budget=10.0)

    assert splits.count("test") == 1  # scored once
    assert splits[-1] == "test"  # and only at the very end
    assert splits[:-1] == ["dev", "dev"]  # the loop itself only ever evaluated dev


# ── revert-if-not: an edit that doesn't help is rolled back ───────────────────────────────


def test_optimize_reverts_an_edit_that_does_not_help(broken: Project) -> None:
    # baseline dev (wrong) → coach → trial dev (STILL wrong) → not improved → revert → converge.
    llm = MockLLMClient(
        [_agent(_CUSTOMERS), _coach_proposal(), _agent(_CUSTOMERS), _agent(_CUSTOMERS)]
    )
    result = optimize(broken, llm=llm, target=0.9, budget=10.0)

    assert result.stopped is StopReason.converged
    assert len(result.frontier) == 1  # only the baseline; nothing was kept
    assert result.picked == 0
    # The rejected edit was rolled back byte-for-byte — the created file is gone.
    assert not (broken.root / "examples" / "learned.yaml").exists()


def test_optimize_reverts_the_file_when_a_trial_eval_crashes(
    broken: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An UNEXPECTED failure after the edit lands (here a trial eval that can't reload the file)
    # must still roll the file back before propagating — a crash must never leave a mutated,
    # un-reverted project, git repo or not (finding #11).
    from sqbyl import optimize as optmod

    real, calls = optmod._eval_dev, {"n": 0}

    def flaky(*a: object, **k: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            return real(*a, **k)  # the baseline eval runs normally
        raise RuntimeError("boom while reloading the just-edited file")

    monkeypatch.setattr(optmod, "_eval_dev", flaky)
    llm = MockLLMClient([_agent(_CUSTOMERS), _coach_proposal(), _agent(_ORDERS)])
    learned = broken.root / "examples" / "learned.yaml"

    with pytest.raises(RuntimeError, match="boom"):
        optimize(broken, llm=llm, target=0.9, budget=10.0)
    assert not learned.exists()  # the applied edit was rolled back despite the crash


# ── budget: the loop hard-stops before a step it can't afford ─────────────────────────────


def test_optimize_hard_stops_on_budget(broken: Project) -> None:
    # A budget too small to afford the coach call after the baseline eval → stop, no rounds.
    # (baseline dev eval + the final held-out eval still run — the loop budget bounds the
    # coach→apply→eval search, not the single before/after measurements.)
    llm = MockLLMClient([_agent(_CUSTOMERS), _agent(_CUSTOMERS)])
    result = optimize(broken, llm=llm, target=0.9, budget=0.0001)

    assert result.stopped is StopReason.budget_exhausted
    assert result.rounds == 0
    assert len(result.frontier) == 1  # never coached, never accepted anything


def test_optimize_surfaces_an_overfitting_gap(broken: Project) -> None:
    # Dev climbs to 100% but the held-out answer is scripted wrong → a dev↔test gap is surfaced.
    llm = MockLLMClient(
        [_agent(_CUSTOMERS), _coach_proposal(), _agent(_ORDERS), _agent(_ORDERS)]  # test wrong
    )
    result = optimize(broken, llm=llm, target=0.9, budget=10.0)
    assert result.picked_point.dev_accuracy == pytest.approx(1.0)
    assert result.test_accuracy == pytest.approx(0.0)
    assert result.dev_test_gap == pytest.approx(1.0)  # the overfitting signal, surfaced


# ── significance, min-gain, frozen clock ──────────────────────────────────────────────────


def test_optimize_flags_a_within_noise_gain_as_not_significant(broken: Project) -> None:
    # A single-question flip on a tiny dev set is a real net gain but NOT statistically
    # distinguishable from noise — the frontier point and the pick must say so (ml-systems).
    llm = MockLLMClient(
        [_agent(_CUSTOMERS), _coach_proposal(), _agent(_ORDERS), _agent(_CUSTOMERS)]
    )
    result = optimize(broken, llm=llm, target=0.9, budget=10.0)
    assert result.frontier[1].net_gain == 1
    assert result.frontier[1].significant is False  # 1 fixed / 0 broke → sign-test p=0.5
    assert result.picked_significant is False
    assert result.edits_tried == 1 and result.edits_kept == 1 and result.edits_reverted == 0


def test_optimize_min_gain_blocks_a_small_win(broken: Project) -> None:
    # Demand at least a 2-question net gain; the 1-question fix no longer clears the bar, so it
    # is reverted and the loop converges at baseline.
    llm = MockLLMClient(
        [_agent(_CUSTOMERS), _coach_proposal(), _agent(_ORDERS), _agent(_CUSTOMERS)]
    )
    result = optimize(broken, llm=llm, target=0.9, budget=10.0, min_gain=2)
    assert result.stopped is StopReason.converged
    assert len(result.frontier) == 1  # nothing cleared the raised bar
    assert result.edits_reverted == 1
    assert not (broken.root / "examples" / "learned.yaml").exists()


def test_optimize_freezes_one_clock_across_the_whole_run(
    broken: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every eval in the loop must share one as_of, so a calendar rollover can't masquerade as an
    # edit effect (ml-systems). Record the as_of each Project.eval was handed.
    seen: list[object] = []
    real_eval = Project.eval

    def _spy(self: Project, split: str = "dev", **kw: object) -> object:
        seen.append(kw.get("as_of"))
        return real_eval(self, split, **kw)

    monkeypatch.setattr(Project, "eval", _spy)
    llm = MockLLMClient(
        [_agent(_CUSTOMERS), _coach_proposal(), _agent(_ORDERS), _agent(_CUSTOMERS)]
    )
    optimize(broken, llm=llm, target=0.9, budget=10.0)
    assert len(set(seen)) == 1 and seen[0] is not None  # one frozen instant, threaded everywhere


# ── the CLI ───────────────────────────────────────────────────────────────────────────────


def test_optimize_cli_requires_a_budget(
    broken: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["optimize", str(broken.root)]) == 2
    assert "requires --budget" in capsys.readouterr().out


def test_optimize_cli_guided_decline_cancels_before_spending(
    broken: Project, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without --auto, the run shows an up-front estimate and asks for consent; answering "n"
    # cancels before a client is built or a token is spent (invariant 5 / responsible-ai).
    monkeypatch.setattr(
        "sqbyl.llm.build_llm_client",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("declined must not build a client")),
    )
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    code = main(["optimize", str(broken.root), "--budget", "10"])
    assert code == 0
    out = capsys.readouterr().out
    assert "estimated spend before you start" in out
    assert "cancelled" in out


def test_optimize_cli_dry_run_spends_nothing(
    broken: Project, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "sqbyl.llm.build_llm_client",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry run must not build a client")),
    )
    assert main(["optimize", str(broken.root), "--dry-run", "--budget", "5"]) == 0
    out = capsys.readouterr().out
    assert "no API calls" in out and "held-out eval" in out


def test_optimize_cli_runs_the_loop(
    broken: Project, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = MockLLMClient(
        [_agent(_CUSTOMERS), _coach_proposal(), _agent(_ORDERS), _agent(_CUSTOMERS)]
    )
    monkeypatch.setattr("sqbyl.llm.build_llm_client", lambda *a, **k: llm)
    # --auto: the headless path (no interactive consent prompt); still requires --budget.
    code = main(["optimize", str(broken.root), "--budget", "10", "--target", "0.9", "--auto"])
    assert code == 0
    out = capsys.readouterr().out
    assert "estimated spend before you start" in out  # up-front consent estimate
    assert "frontier" in out and "picked v1" in out
    assert "held-out test 100%" in out
