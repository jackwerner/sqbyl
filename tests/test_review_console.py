"""Phase 5.2 — the judge review surface on the console (spec §7).

The graded behaviour (plan 5.2): each judged row shows the question, generated SQL, gold,
and the judge's verdict *with rationale*; a human confirms or overrides. An override is
authoritative — it flips the run's **resolved** accuracy (never the deterministic floor) —
and it feeds the calibration set, so the live judge↔human agreement score updates. Driven
with Starlette's in-process TestClient.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from sqbyl.calibration_io import few_shot_examples, judge_agreement, load_calibration
from sqbyl.console import create_app
from sqbyl.eval.benchmarks_io import Split, benchmark_path
from sqbyl.eval.report import latest_run, save_run
from sqbyl.models import JudgeVerdict, QuestionResult, ScoredRun, Verdict
from sqbyl.project import Project
from sqbyl_runtime.state.layout import SqbylPaths

if TYPE_CHECKING:
    from starlette.testclient import TestClient


def _q(qid: str, verdict: Verdict, *, suggestion: Verdict | None = None) -> QuestionResult:
    verdicts = (
        [
            JudgeVerdict(
                judge="semantic_equivalence",
                passed=suggestion is Verdict.correct,
                confidence=0.9,
                rationale="the two queries compute the same thing",
            )
        ]
        if suggestion is not None
        else []
    )
    return QuestionResult(
        id=qid,
        question=f"question {qid}?",
        verdict=verdict,
        generated_sql=f"SELECT '{qid}'",
        gold_sql=f"SELECT '{qid}' AS x",
        judge_suggestion=suggestion,
        judge_verdicts=verdicts,
    )


@pytest.fixture
def project(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Project:
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))
    dst = tmp_path / "proj"
    shutil.copytree(dogfood_dir, dst)
    project = Project.load(dst)
    # A persisted dev run: one deterministic pass, and a review pile the judge has triaged.
    run = ScoredRun(
        run_id="run_5_2_abc",
        split="dev",
        models={"agent": "claude-x", "judge": "claude-x"},
        results=[
            _q("q_ok", Verdict.correct),
            _q("q_equiv", Verdict.manual_review, suggestion=Verdict.correct),
            _q("q_wrong", Verdict.manual_review, suggestion=Verdict.incorrect),
            _q("q_nojudge", Verdict.manual_review, suggestion=None),
        ],
    )
    save_run(SqbylPaths(dst).ensure(), run)
    return project


@pytest.fixture
def client(project: Project) -> TestClient:
    from starlette.testclient import TestClient

    return TestClient(create_app(project))


def test_review_lists_the_pile_with_judge_rationale(client: TestClient) -> None:
    data = client.get("/api/review?split=dev").json()
    # Only the review pile (manual_review) rows, not the deterministically-correct one.
    ids = {r["id"] for r in data["rows"]}
    assert ids == {"q_equiv", "q_wrong", "q_nojudge"}
    equiv = next(r for r in data["rows"] if r["id"] == "q_equiv")
    assert equiv["judge_suggestion"] == "correct"
    assert "compute the same thing" in equiv["judge_verdicts"][0]["rationale"]  # the "why"
    # Deterministic floor is 1/4; nothing reviewed yet, so resolved == deterministic.
    assert data["run"]["accuracy"] == 0.25
    assert data["run"]["resolved_accuracy"] == 0.25
    assert data["run"]["agreement"]["rate"] is None  # no reviews → no agreement claim


def test_override_flips_resolved_accuracy_not_the_deterministic_floor(
    project: Project, client: TestClient
) -> None:
    # Human confirms a likely-equivalent row as correct → resolved climbs 0.25 → 0.50.
    resp = client.post("/api/review/q_equiv/resolve", json={"verdict": "correct", "split": "dev"})
    body = resp.json()
    assert body["ok"] is True
    assert body["run"]["accuracy"] == 0.25  # deterministic floor unmoved (spec §7)
    assert body["run"]["resolved_accuracy"] == 0.5  # human-trusted number flips
    assert body["run"]["n_reviewed"] == 1

    # The call is authoritative and persisted to the run on disk.
    run = latest_run(SqbylPaths(project.root), split="dev")
    assert run is not None
    row = next(r for r in run.results if r.id == "q_equiv")
    assert row.human_verdict is Verdict.correct


def test_confirm_and_override_feed_the_calibration_set(
    project: Project, client: TestClient
) -> None:
    # Agree with the judge on q_equiv (it suggested correct) …
    client.post("/api/review/q_equiv/resolve", json={"verdict": "correct"})
    # … and override the judge on q_wrong (it suggested incorrect, human says correct).
    last = client.post("/api/review/q_wrong/resolve", json={"verdict": "correct"}).json()

    records = load_calibration(project)
    by_id = {r.question_id: r for r in records}
    assert by_id["q_equiv"].agreed is True  # human matched the suggestion
    assert by_id["q_wrong"].agreed is False  # human overrode it
    # Live agreement over the calibration set: 1 of 2.
    assert last["run"]["agreement"] == {"n": 2, "n_agree": 1, "rate": 0.5}


def test_resolving_a_row_with_no_judge_is_not_calibration_data(
    project: Project, client: TestClient
) -> None:
    # A row no judge triaged can still be resolved, but it isn't judge-calibration data.
    client.post("/api/review/q_nojudge/resolve", json={"verdict": "correct"})
    assert load_calibration(project) == []  # nothing to calibrate the judge against
    run = latest_run(SqbylPaths(project.root), split="dev")
    assert run is not None
    assert next(r for r in run.results if r.id == "q_nojudge").human_verdict is Verdict.correct


def test_unknown_question_and_missing_run_are_404(project: Project, client: TestClient) -> None:
    assert client.post("/api/review/nope/resolve", json={"verdict": "correct"}).status_code == 404
    assert (
        client.post(
            "/api/review/q_ok/resolve", json={"verdict": "correct", "split": "test"}
        ).status_code
        == 404
    )


def test_review_never_touches_the_held_out_set(project: Project, client: TestClient) -> None:
    before = benchmark_path(project, Split.test).read_text()
    client.post("/api/review/q_equiv/resolve", json={"verdict": "correct"})
    assert benchmark_path(project, Split.test).read_text() == before  # invariant 3


def test_resolution_becomes_a_few_shot_example(project: Project, client: TestClient) -> None:
    # A review carries the concrete case forward so the judge can be coached with it (spec §7).
    client.post(
        "/api/review/q_wrong/resolve",
        json={"verdict": "correct", "note": "different SQL, same customers"},
    )
    examples = few_shot_examples(project, split="dev")
    assert len(examples) == 1
    ex = examples[0]
    assert ex.split == "dev"  # stamped from the run's split
    assert ex.question == "question q_wrong?"  # the case, not just ids
    assert ex.generated_sql == "SELECT 'q_wrong'"
    assert ex.human_verdict is Verdict.correct
    assert ex.note == "different SQL, same customers"


def test_a_test_split_ruling_never_coaches_the_dev_judge(
    project: Project, client: TestClient
) -> None:
    # invariant 3: reviewing a held-out test row must not leak into the dev-loop judge.
    paths = SqbylPaths(project.root)
    test_run = ScoredRun(
        run_id="run_test_xyz",
        split="test",
        results=[_q("t_equiv", Verdict.manual_review, suggestion=Verdict.correct)],
    )
    save_run(paths, test_run)

    client.post("/api/review/t_equiv/resolve", json={"verdict": "correct", "split": "test"})

    # The ruling is recorded and calibrates the *test* judge, but is invisible to dev's.
    assert few_shot_examples(project, split="test")  # present for test
    assert few_shot_examples(project, split="dev") == []  # never leaks to dev (invariant 3)


def test_re_resolving_does_not_double_count_agreement(project: Project, client: TestClient) -> None:
    # A human changes their mind: only the latest ruling per row counts (append-only trail,
    # deduped aggregate) — a repeated click can't inflate the agreement denominator.
    client.post("/api/review/q_equiv/resolve", json={"verdict": "correct"})  # agrees with judge
    client.post("/api/review/q_equiv/resolve", json={"verdict": "incorrect"})  # changes mind

    assert len(load_calibration(project)) == 2  # both appended (audit trail)
    ag = judge_agreement(project, split="dev")
    assert ag.n == 1  # but only the latest ruling for the row counts
    assert ag.n_agree == 0  # the final call (incorrect) disagreed with the suggestion
