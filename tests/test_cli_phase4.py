"""Phase 4 CLI surface — `sqbyl synth`.

A paid command: prints an up-front estimate, meters the drafting call to
``.sqbyl/usage.db`` (invariant 5), and writes survivors to the review queue — never to a
benchmark file (accept in the console does that). Driven with an injected mock client.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sqbyl.candidates_io import load_candidates
from sqbyl.cli import main
from sqbyl.eval.benchmarks_io import Split, benchmark_path
from sqbyl_runtime.llm.mock import MockLLMClient, structured_reply
from sqbyl_runtime.state.layout import SqbylPaths
from sqbyl_runtime.state.usage import UsageStore

_BATCH = {
    "questions": [
        {
            "question": "How many orders?",
            "gold_sql": "SELECT COUNT(*) FROM analytics.orders",
            "difficulty": "easy",
        },
        {"question": "Broken", "gold_sql": "SELECT nope FROM analytics.orders"},
    ]
}


@pytest.fixture
def project(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused-in-mock")
    dst = tmp_path / "proj"
    shutil.copytree(dogfood_dir, dst)

    def _factory(*_a: object, **_k: object) -> MockLLMClient:
        return MockLLMClient([structured_reply(_BATCH)])

    monkeypatch.setattr("sqbyl.llm.build_llm_client", _factory)
    return dst


def test_synth_cli_grounds_meters_and_queues(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from sqbyl.project import Project

    test_before = benchmark_path(Project.load(project), Split.test).read_text()
    code = main(["synth", str(project), "--n", "2"])
    assert code == 0

    out = capsys.readouterr().out
    assert "estimated ~$" in out  # up-front estimate (invariant 5)
    assert "kept 1" in out and "dropped 1" in out  # execution-grounded drop is visible
    assert "sqbyl review" in out  # points the user at the console

    # The drafting call was metered under the synth command.
    with UsageStore(SqbylPaths(project).usage_db) as store:
        rows = store.all()
    assert rows and all(r.command == "synth" for r in rows)

    # The survivor is queued for review, not written to any benchmark file.
    queued = load_candidates(Project.load(project))
    assert [c.id for c in queued if c.status.value == "pending"]
    assert benchmark_path(Project.load(project), Split.test).read_text() == test_before


def test_synth_cli_refuses_when_estimate_exceeds_budget(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # --auto hard-stops when the up-front estimate exceeds the cap (guided would pause-and-ask).
    code = main(["synth", str(project), "--n", "2", "--auto", "--budget", "0.0001"])
    assert code == 1
    out = capsys.readouterr().out
    assert "exceeds budget" in out
    # Nothing was queued because we bailed before the paid call.
    from sqbyl.project import Project

    assert load_candidates(Project.load(project)) == []
