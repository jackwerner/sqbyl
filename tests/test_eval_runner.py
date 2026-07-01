"""Phase 3.1 — the eval runner, mock-first plus a record-replay fixture (invariant 4).

The runner runs each benchmark question as a fresh, stateless ``ask()``. Scripting the
agent to answer with each question's own gold SQL makes every ``result_correctness``
comparison pass, so the harness itself is exercised end-to-end with zero tokens.

Regenerate the cassette after an intentional prompt/context change:
    SQBYL_UPDATE_CASSETTES=1 uv run pytest tests/test_eval_runner.py
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from sqbyl.eval.benchmarks_io import Split, load_dev_set
from sqbyl.eval.runner import run_eval
from sqbyl.models import BenchmarkQuestion
from sqbyl.models.runs import Verdict
from sqbyl.project import Project
from sqbyl_runtime.llm.mock import MockLLMClient, structured_reply
from sqbyl_runtime.llm.replay import RecordReplayLLMClient

_CASSETTE = Path(__file__).resolve().parent / "cassettes" / "eval_dev.json"


@pytest.fixture(autouse=True)
def _fixture_db(duckdb_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The dogfood manifest points at env:DATABASE_URL; aim it at the seeded fixture.
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))


def _gold_replies(questions: list[BenchmarkQuestion]) -> list[object]:
    """One scripted reply per question, answering with that question's gold SQL."""
    return [
        structured_reply({"plan": f"answer {q.id}", "sql": q.gold_sql, "used_assets": []})
        for q in questions
    ]


def test_runner_scores_the_dev_set_all_correct(dogfood_dir: Path) -> None:
    project = Project.load(dogfood_dir)
    questions = load_dev_set(project)
    mock = MockLLMClient(_gold_replies(questions))

    run = run_eval(project, split=Split.dev, llm=mock)

    assert run.split == "dev"
    assert run.total == len(questions)
    assert run.accuracy == 1.0
    assert run.n_manual_review == 0
    assert run.models["agent"]  # stamped with the agent model (spec §7)
    assert run.as_of is not None  # clock frozen for reproducibility


def test_runner_stamps_a_pinned_as_of(dogfood_dir: Path) -> None:
    project = Project.load(dogfood_dir)
    questions = load_dev_set(project)
    pinned = datetime(2026, 6, 30, 12, 0, 0)
    run = run_eval(
        project, split=Split.dev, llm=MockLLMClient(_gold_replies(questions)), as_of=pinned
    )
    assert run.as_of == pinned  # pinned clock is recorded for reproducibility


def test_runner_routes_a_wrong_answer_to_manual_review(dogfood_dir: Path) -> None:
    project = Project.load(dogfood_dir)
    questions = load_dev_set(project)
    replies = _gold_replies(questions)
    # Sabotage the first question's SQL so its result set won't match gold.
    replies[0] = structured_reply(
        {"plan": "wrong", "sql": "SELECT COUNT(*) + 1 FROM analytics.orders", "used_assets": []}
    )
    run = run_eval(project, split=Split.dev, llm=MockLLMClient(replies))

    first = run.results[0]
    assert first.verdict is Verdict.manual_review  # mismatch is never asserted "incorrect"
    assert run.n_manual_review == 1
    assert run.accuracy == (len(questions) - 1) / len(questions)


def _write_cassette(project: Project, questions: list[BenchmarkQuestion]) -> None:
    replies = _gold_replies(questions)
    capture = MockLLMClient(replies)
    run_eval(project, split=Split.dev, llm=capture)
    entries = {
        req.fingerprint(): {
            "request": req.model_dump(mode="json"),
            "response": reply.model_dump(mode="json"),  # type: ignore[attr-defined]
        }
        for req, reply in zip(capture.requests, replies, strict=True)
    }
    _CASSETTE.parent.mkdir(parents=True, exist_ok=True)
    _CASSETTE.write_text(
        json.dumps({"version": 1, "entries": entries}, indent=2, sort_keys=True) + "\n"
    )


def test_runner_replays_the_dev_set_from_cassette(dogfood_dir: Path) -> None:
    project = Project.load(dogfood_dir)
    questions = load_dev_set(project)
    if os.environ.get("SQBYL_UPDATE_CASSETTES") or not _CASSETTE.exists():
        _write_cassette(project, questions)

    client = RecordReplayLLMClient(_CASSETTE, mode="replay")
    run = run_eval(project, split=Split.dev, llm=client)

    assert run.accuracy == 1.0
    assert run.total == len(questions)
    assert run.total_cost_usd >= 0.0
