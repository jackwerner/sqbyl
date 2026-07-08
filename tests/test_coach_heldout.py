"""Finding #3 — guardrailed diagnosis of a held-out failure (`coach --from-test-failure`).

The graded guardrails: the gold is walled off by construction (the diagnoser's input type has
no gold field, and the rendered prompt never contains the gold); proposals are stamped with
their held-out provenance; and inspecting an item quarantines its score. The import-linter
separately proves `sqbyl.coach_heldout` can't reach `sqbyl.eval.heldout`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sqbyl.coach_heldout import (
    HeldoutFailure,
    _render_heldout_prompt,
    coach_heldout_failure,
    load_quarantine,
    quarantined_ids,
    record_quarantine,
)
from sqbyl.models import QuestionResult, Verdict
from sqbyl.project import Project
from sqbyl_runtime.llm.base import Usage
from sqbyl_runtime.llm.mock import MockLLMClient, structured_reply
from sqbyl_runtime.state.layout import SqbylPaths

_SECRET_GOLD = "SELECT secret_gold_marker FROM analytics.orders WHERE status='confirmed'"


def _project(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, mp: pytest.MonkeyPatch
) -> Project:
    mp.setenv("DATABASE_URL", str(duckdb_path))
    dst = tmp_path / "proj"
    shutil.copytree(dogfood_dir, dst)
    return Project.load(dst)


def _test_row() -> QuestionResult:
    # A held-out failure carrying a distinctive gold marker we then prove never leaks.
    return QuestionResult(
        id="t_price",
        question="What is the average rating for products that cost more than $100?",
        verdict=Verdict.manual_review,
        plan="filter by cost_price then average rating",
        generated_sql="SELECT AVG(rating) FROM products WHERE cost_price > 100",
        gold_sql=_SECRET_GOLD,
        selected_tables=["products"],
    )


def test_heldout_failure_has_no_gold_fields() -> None:
    # The wall: the diagnoser's input type structurally cannot carry the gold.
    fields = set(HeldoutFailure.model_fields)
    assert "gold_sql" not in fields
    assert "gold_asset" not in fields


def test_from_question_result_drops_the_gold() -> None:
    failure = HeldoutFailure.from_question_result(_test_row())
    assert failure.id == "t_price"
    assert failure.generated_sql == "SELECT AVG(rating) FROM products WHERE cost_price > 100"
    # Serialize the whole thing and confirm the gold marker is nowhere in it.
    assert "secret_gold_marker" not in failure.model_dump_json()


def test_rendered_prompt_never_contains_the_gold(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, dogfood_dir, duckdb_path, monkeypatch)
    failure = HeldoutFailure.from_question_result(_test_row())
    prompt = _render_heldout_prompt(project, failure, dialect=project.manifest.database.dialect)
    assert "secret_gold_marker" not in prompt
    assert "withheld" in prompt  # it tells the model the gold is intentionally absent
    assert failure.generated_sql in prompt  # but the agent's own trace IS shown


def test_coach_heldout_stamps_provenance(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, dogfood_dir, duckdb_path, monkeypatch)
    example = "- question: products by rating\n  sql: SELECT name, avg_rating FROM products\n"
    client = MockLLMClient(
        [
            structured_reply(
                {
                    "proposals": [
                        {
                            "title": "clarify cost_price vs unit_price",
                            "root_cause": "the agent read 'cost' as cost_price, not unit_price",
                            "layer": "example",
                            "target_file": "examples/learned.yaml",
                            "edits": [{"find": "", "replace": example}],
                            "predicted_fixes": 1,
                            "confidence": 0.7,
                        }
                    ]
                },
                usage=Usage(input_tokens=400, output_tokens=60),
            )
        ]
    )
    failure = HeldoutFailure.from_question_result(_test_row())
    report = coach_heldout_failure(project, failure, llm=client, model="claude-x")
    assert report.run_id == "heldout:t_price"
    assert report.proposals
    assert all(p.derived_from_heldout == "t_price" for p in report.proposals)
    assert all(p.question_ids == ["t_price"] for p in report.proposals)


def test_quarantine_roundtrip(tmp_path: Path) -> None:
    paths = SqbylPaths(tmp_path).ensure()
    assert quarantined_ids(paths) == set()
    record_quarantine(paths, "t_price", reason="inspected to coach")
    record_quarantine(paths, "t_other")
    assert quarantined_ids(paths) == {"t_price", "t_other"}
    records = {r.question_id: r for r in load_quarantine(paths)}
    assert records["t_price"].reason == "inspected to coach"
    # Idempotent: re-recording refreshes rather than duplicating.
    record_quarantine(paths, "t_price", reason="again")
    assert len(load_quarantine(paths)) == 2
    assert {r.question_id for r in load_quarantine(paths)} == {"t_price", "t_other"}
