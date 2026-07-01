"""Phase 4.1 — the execution-grounded synthesizer, mock-first + a record-replay fixture.

The graded behaviours (plan 4.1): candidates whose gold SQL doesn't run are provably
dropped; survivors carry the executed rows as review evidence; nothing is ever written to
``test.yaml``. All driven with a scripted client, zero tokens (invariant 4).

Regenerate the cassette after an intentional prompt change:
    SQBYL_UPDATE_CASSETTES=1 uv run pytest tests/test_synth.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sqbyl.eval.benchmarks_io import Split, benchmark_path
from sqbyl.models import DropReason
from sqbyl.project import Project
from sqbyl.projectfiles import load_semantics
from sqbyl.synth import DraftQuestion, ground_candidates, plan_seeds, synthesize
from sqbyl_runtime.llm.mock import MockLLMClient, structured_reply
from sqbyl_runtime.llm.replay import RecordReplayLLMClient

_CASSETTE = Path(__file__).resolve().parent / "cassettes" / "synth_dev.json"


@pytest.fixture(autouse=True)
def _fixture_db(duckdb_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))


# A batch mixing keepers with every drop reason, for grounding tests.
_DRAFTS = [
    {
        "question": "How many orders are there?",
        "gold_sql": "SELECT COUNT(*) FROM analytics.orders",
        "difficulty": "easy",
        "canonical": True,
        "group": "orders_total",
    },
    {
        "question": "Total order count please",
        "gold_sql": "SELECT COUNT(*) AS n FROM analytics.orders",
        "difficulty": "easy",
        "canonical": False,
        "group": "orders_total",
    },
    {
        "question": "Revenue by region",
        "gold_sql": "SELECT c.region, SUM(o.amount_cents)/100.0 AS r FROM analytics.orders o "
        "JOIN analytics.customers c ON o.customer_id=c.customer_id GROUP BY c.region",
        "difficulty": "hard",
    },
    {"question": "Nonexistent column", "gold_sql": "SELECT nope FROM analytics.orders"},
    {
        "question": "Impossible filter",
        "gold_sql": "SELECT * FROM analytics.orders WHERE status='does-not-exist'",
    },
    {"question": "A write", "gold_sql": "DELETE FROM analytics.orders"},
    {"question": "All null", "gold_sql": "SELECT NULL AS x WHERE 1=1"},
]


def _batch_reply() -> object:
    return structured_reply({"questions": _DRAFTS})


def test_plan_seeds_covers_tables_measures_filters_and_joins(dogfood_dir: Path) -> None:
    seeds = plan_seeds(load_semantics(Project.load(dogfood_dir)))
    labels = {s.label for s in seeds}
    assert any(label.startswith("table:") for label in labels)
    assert "measure:analytics.orders.net_revenue" in labels
    assert "filter:analytics.orders.last_quarter" in labels
    assert any(label.startswith("join:") for label in labels)
    # Difficulty is stratified, not uniform.
    assert {s.difficulty for s in seeds} >= {"easy", "medium", "hard"}


def test_grounding_drops_every_non_runnable_candidate(dogfood_dir: Path) -> None:
    project = Project.load(dogfood_dir)
    drafts = [DraftQuestion.model_validate(d) for d in _DRAFTS]
    dialect = project.manifest.database.dialect
    with project.connect() as db:
        survivors, dropped = ground_candidates(db, drafts, dialect=dialect)

    kept = {c.question for c in survivors}
    assert kept == {"How many orders are there?", "Total order count please", "Revenue by region"}
    reasons = {d.question: d.reason for d in dropped}
    assert reasons["Nonexistent column"] is DropReason.syntax_error
    assert reasons["Impossible filter"] is DropReason.empty_result
    assert reasons["A write"] is DropReason.syntax_error  # non-SELECT refused by the guard
    assert reasons["All null"] is DropReason.degenerate


def test_survivors_carry_executed_evidence_and_variant_links(dogfood_dir: Path) -> None:
    project = Project.load(dogfood_dir)
    drafts = [DraftQuestion.model_validate(d) for d in _DRAFTS]
    with project.connect() as db:
        survivors, _ = ground_candidates(db, drafts, dialect=project.manifest.database.dialect)

    by_q = {c.question: c for c in survivors}
    canonical = by_q["How many orders are there?"]
    variant = by_q["Total order count please"]
    assert canonical.evidence.row_count >= 1  # the rows a human eyeballs in review
    assert canonical.evidence.columns  # column names captured
    assert variant.canonical is False
    assert variant.variant_of == canonical.id  # phrasing variant linked to its canonical


def test_synthesize_writes_no_benchmark_file(dogfood_dir: Path, tmp_path: Path) -> None:
    # synth produces a SynthResult only; it must never write dev.yaml or test.yaml itself
    # (accept in the console is the sole writer). Assert the held-out set is untouched.
    import shutil

    dst = tmp_path / "proj"
    shutil.copytree(dogfood_dir, dst)
    project = Project.load(dst)
    test_before = benchmark_path(project, Split.test).read_text()
    dev_before = benchmark_path(project, Split.dev).read_text()

    result = synthesize(project, llm=MockLLMClient([_batch_reply()]), model="claude-opus-4-8", n=7)

    assert result.n_survivors == 3
    assert result.n_dropped == 4
    assert benchmark_path(project, Split.test).read_text() == test_before  # never touched
    assert benchmark_path(project, Split.dev).read_text() == dev_before  # synth doesn't write it


def test_ids_do_not_collide_across_separate_synth_runs(dogfood_dir: Path, tmp_path: Path) -> None:
    # Two different questions that slug to the same base, produced in separate runs, must
    # get distinct ids — otherwise the second silently overwrites the first in the queue
    # and is skipped as an "idempotent" re-accept, losing dev-set data.
    import shutil

    from sqbyl.candidates_io import add_candidates, load_candidates

    dst = tmp_path / "proj"
    shutil.copytree(dogfood_dir, dst)
    project = Project.load(dst)

    def _run(question: str) -> None:
        batch = {
            "questions": [
                {"question": question, "gold_sql": "SELECT COUNT(*) FROM analytics.orders"}
            ]
        }
        result = synthesize(
            project, llm=MockLLMClient([structured_reply(batch)]), model="claude-opus-4-8", n=1
        )
        add_candidates(project, result.survivors)

    _run("How many orders are there???")  # slugs to q_how_many_orders_are_there
    _run("How many orders are there!!!")  # same base — must not collide

    ids = [c.id for c in load_candidates(project)]
    assert len(ids) == 2  # both retained
    assert len(set(ids)) == 2  # genuinely distinct ids


def _write_cassette(project: Project) -> None:
    capture = MockLLMClient([_batch_reply()])
    synthesize(project, llm=capture, model="claude-opus-4-8", n=7)
    entries = {
        req.fingerprint(): {
            "request": req.model_dump(mode="json"),
            "response": _batch_reply().model_dump(mode="json"),  # type: ignore[attr-defined]
        }
        for req in capture.requests
    }
    _CASSETTE.parent.mkdir(parents=True, exist_ok=True)
    _CASSETTE.write_text(
        json.dumps({"version": 1, "entries": entries}, indent=2, sort_keys=True) + "\n"
    )


def test_synthesize_replays_from_cassette(dogfood_dir: Path) -> None:
    project = Project.load(dogfood_dir)
    if os.environ.get("SQBYL_UPDATE_CASSETTES") or not _CASSETTE.exists():
        _write_cassette(project)
    client = RecordReplayLLMClient(_CASSETTE, mode="replay")
    result = synthesize(project, llm=client, model="claude-opus-4-8", n=7)
    assert result.n_survivors == 3
    assert result.n_dropped == 4
