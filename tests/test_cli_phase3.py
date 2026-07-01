"""Phase 3 CLI surface — `sqbyl eval`.

A paid command: prints an up-front estimate, meters every ``ask()`` to ``.sqbyl/usage.db``
(invariant 5), persists the run to ``.sqbyl/runs/``, and prints the flipped-questions diff
on a second run. Driven with no key via an injected mock client scripted to answer each
question with its gold SQL (so every result_correctness comparison passes).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sqbyl.cli import main
from sqbyl.eval.benchmarks_io import Split, load_dev_set
from sqbyl.eval.report import load_runs
from sqbyl.project import Project
from sqbyl_runtime.llm.mock import MockLLMClient, structured_reply
from sqbyl_runtime.state.layout import SqbylPaths
from sqbyl_runtime.state.usage import UsageStore


@pytest.fixture
def project(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused-in-mock")
    dst = tmp_path / "proj"
    shutil.copytree(dogfood_dir, dst)
    return dst


def _patch_gold_client(project_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make build_llm_client return a FRESH gold-answering mock on each call."""
    questions = load_dev_set(Project.load(project_dir))

    def _factory(*_a: object, **_k: object) -> MockLLMClient:
        return MockLLMClient(
            [
                structured_reply({"plan": q.id, "sql": q.gold_sql, "used_assets": []})
                for q in questions
            ]
        )

    monkeypatch.setattr("sqbyl.llm.build_llm_client", _factory)


def test_eval_cli_scores_meters_and_persists(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_gold_client(project, monkeypatch)
    n = len(load_dev_set(Project.load(project)))

    code = main(["eval", "dev", str(project)])
    assert code == 0
    out = capsys.readouterr().out
    assert "estimated ~$" in out  # up-front estimate (invariant 5)
    assert f"accuracy: {n}/{n} (100.0%" in out  # followed by the 95% CI
    assert "95% CI" in out

    # Every ask() was metered under the eval command.
    with UsageStore(SqbylPaths(project).usage_db) as store:
        rows = store.all()
    assert len(rows) == n
    assert all(r.command == "eval" for r in rows)

    # The run persisted and reloads.
    runs = load_runs(SqbylPaths(project), split="dev")
    assert len(runs) == 1
    assert runs[0].accuracy == 1.0


def test_eval_cli_second_run_reports_no_flips(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_gold_client(project, monkeypatch)
    assert main(["eval", "dev", str(project)]) == 0
    capsys.readouterr()  # drop first-run output
    assert main(["eval", "dev", str(project)]) == 0
    out = capsys.readouterr().out
    assert "no questions flipped" in out
    # Two runs now persisted for the dev split.
    assert len(load_runs(SqbylPaths(project), split="dev")) == 2


def test_eval_cli_defaults_to_dev_and_accepts_test(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `test` split is readable by eval (the one caller allowed); mock answers with the
    # held-out gold SQL.
    from sqbyl.eval.heldout import load_for_eval

    test_qs = load_for_eval(Project.load(project), Split.test)

    def _factory(*_a: object, **_k: object) -> MockLLMClient:
        return MockLLMClient(
            [
                structured_reply({"plan": q.id, "sql": q.gold_sql, "used_assets": []})
                for q in test_qs
            ]
        )

    monkeypatch.setattr("sqbyl.llm.build_llm_client", _factory)
    code = main(["eval", "test", str(project)])
    assert code == 0
    out = capsys.readouterr().out
    assert f"accuracy: {len(test_qs)}/{len(test_qs)}" in out
    assert "held-out test scored" not in out  # first scoring: no peek warning yet
    assert len(load_runs(SqbylPaths(project), split="test")) == 1

    # Scoring the held-out set again warns about peeking (spec §7 leakage guard).
    assert main(["eval", "test", str(project)]) == 0
    assert "held-out test scored 1 time(s) before" in capsys.readouterr().out
