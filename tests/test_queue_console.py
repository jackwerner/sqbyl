"""Phase 6.3 — the attention queue on the console (spec §5.5, plan 6.3).

The console opens onto the leverage-sorted attention queue: a live readiness meter, the
high-confidence work staged for one-click apply, and the cards that need a human first. The
"done when": the dogfood project produces a §5.5-shaped queue, and accepting a card moves the
meter live (applies a Coach edit / resolves a judge row, and the card drops out).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from sqbyl.console import create_app
from sqbyl.eval.report import load_runs, save_run
from sqbyl.models import (
    CoachEdit,
    CoachLayer,
    CoachProposal,
    CoachReport,
    JudgeVerdict,
    QuestionResult,
    ScoredRun,
    Verdict,
)
from sqbyl.project import Project
from sqbyl_runtime.state.layout import SqbylPaths

if TYPE_CHECKING:
    from starlette.testclient import TestClient


def _q(qid: str, verdict: Verdict, *, suggestion: Verdict | None = None) -> QuestionResult:
    verdicts = (
        [JudgeVerdict(judge="semantic_equivalence", passed=True, confidence=0.8, rationale="≈")]
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


def _proposal(pid: str, *, confidence: float, fixes: int, qids: list[str]) -> CoachProposal:
    # Applyable to the dogfood tree: an empty `find` appends (creates) a file in the examples/
    # allowlisted dir; empty target_fingerprint skips the drift check.
    return CoachProposal(
        id=pid,
        title=f"Add measure {pid}",
        root_cause="model doesn't know the revenue definition",
        layer=CoachLayer.measure,
        target_file=f"examples/coach_{pid}.yaml",
        edits=[CoachEdit(find="", replace=f"measure: {pid}\n")],
        predicted_fixes=fixes,
        confidence=confidence,
        question_ids=qids,
    )


@pytest.fixture
def project(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Project:
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))
    dst = tmp_path / "proj"
    shutil.copytree(dogfood_dir, dst)
    project = Project.load(dst)
    paths = SqbylPaths(dst).ensure()

    # A dev run: 6 deterministic questions, 3 correct, 2 in the review pile, plus context.
    run = ScoredRun(
        run_id="run_6_3",
        split="dev",
        models={"agent": "claude-x", "judge": "claude-x"},
        results=[
            _q("a", Verdict.correct),
            _q("b", Verdict.correct),
            _q("c", Verdict.correct),
            _q("d", Verdict.incorrect),
            _q("e", Verdict.manual_review, suggestion=Verdict.correct),
            _q("f", Verdict.manual_review, suggestion=Verdict.incorrect),
        ],
    )
    save_run(paths, run)

    # A coach report: one high-confidence (auto-apply bucket) + one queued proposal.
    from sqbyl.coach import save_report

    report = CoachReport(
        run_id="run_6_3",
        proposals=[
            _proposal("hi", confidence=0.95, fixes=1, qids=["d"]),
            _proposal("lo", confidence=0.50, fixes=1, qids=["d"]),
        ],
    )
    save_report(paths, report)
    return project


@pytest.fixture
def client(project: Project) -> TestClient:
    from starlette.testclient import TestClient

    return TestClient(create_app(project))


def test_queue_has_the_555_shape(client: TestClient) -> None:
    data = client.get("/api/queue").json()

    # Readiness meter: measured accuracy 3/6 = 50%, target 95%, projection labelled.
    r = data["readiness"]
    assert r["accuracy"] == pytest.approx(0.5)
    assert r["target"] == pytest.approx(0.95)
    # The projection is labelled as an estimate (the "~" marker) — never dressed up as measured.
    assert "50%" in r["headline"] and "~" in r["headline"]
    assert r["low_confidence"] is True  # n=6 < SMALL_N_FLOOR

    # High-confidence coach proposal staged for apply; low-confidence one queued.
    assert [d["id"] for d in data["auto_applied"]] == ["coach:hi"]

    ids = [d["id"] for d in data["queue"]]
    assert "coach:lo" in ids  # the queued coach card
    assert any(i.startswith("judge:") for i in ids)  # the two review-pile cards

    # Leverage-sorted: the coach card (leverage > 0) precedes the zero-leverage judge cards.
    coach_pos = ids.index("coach:lo")
    judge_pos = min(i for i, x in enumerate(ids) if x.startswith("judge:"))
    assert coach_pos < judge_pos


def test_accepting_a_coach_card_applies_it_and_moves_the_meter(
    client: TestClient, project: Project
) -> None:
    before = client.get("/api/queue").json()
    assert "coach:lo" in [d["id"] for d in before["queue"]]

    res = client.post("/api/queue/coach:lo/accept").json()
    assert res["ok"] is True

    # The edit landed in the project tree…
    assert (project.root / "examples" / "coach_lo.yaml").read_text() == "measure: lo\n"
    # …and the card dropped out of the returned (fresh) queue — the meter moved live.
    assert "coach:lo" not in [d["id"] for d in res["queue"]]
    assert res["readiness"]["accuracy"] == pytest.approx(
        0.5
    )  # measured floor unchanged (no re-eval)


def test_accepting_a_judge_card_resolves_the_row(client: TestClient) -> None:
    before = client.get("/api/queue").json()
    judge_id = next(
        d["id"] for d in before["queue"] if d["id"].startswith("judge:") and d["id"].endswith(":e")
    )

    res = client.post(f"/api/queue/{judge_id}/accept").json()
    assert res["ok"] is True
    # Confirming the judge's "correct" suggestion on q_e leaves the review pile and lifts
    # resolved accuracy (from 3/6 to 4/6) without touching the deterministic floor.
    assert judge_id not in [d["id"] for d in res["queue"]]


def test_applied_coach_proposal_is_not_reoffered(client: TestClient) -> None:
    client.post("/api/queue/coach:hi/accept")
    data = client.get("/api/queue").json()
    # Once applied, the high-confidence proposal leaves both buckets (applied_at is set).
    assert "coach:hi" not in [d["id"] for d in data["auto_applied"]]
    assert "coach:hi" not in [d["id"] for d in data["queue"]]


def test_unknown_decision_is_rejected(client: TestClient) -> None:
    assert client.post("/api/queue/coach:nope/accept").status_code == 404
    assert client.post("/api/queue/mystery:1/accept").status_code == 422


def test_queue_never_surfaces_or_resolves_the_test_split(project: Project) -> None:
    # Seed a held-out *test* run alongside the dev one. The queue is dev-only (invariant 3): it
    # must never assemble a test row, and an accept must never resolve against the test run.
    from starlette.testclient import TestClient

    paths = SqbylPaths(project.root)
    test_run = ScoredRun(
        run_id="run_6_3_TEST",
        split="test",
        models={"agent": "claude-x"},
        results=[_q("t1", Verdict.manual_review, suggestion=Verdict.correct)],
    )
    save_run(paths, test_run)
    client = TestClient(create_app(project))

    data = client.get("/api/queue").json()
    ids = [d["id"] for d in data["queue"]]
    assert all("run_6_3_TEST" not in i for i in ids)  # no test row surfaced
    assert not any(i.endswith(":t1") for i in ids)

    # Even a hand-crafted id pointing at the test run can't resolve it — the accept path is
    # pinned to the dev run, so its prefix check fails closed.
    assert client.post("/api/queue/judge:run_6_3_TEST:t1/accept").status_code == 404
    # The test run file is untouched (still one unreviewed row, no human verdict written).
    reloaded = next(r for r in load_runs(paths) if r.run_id == "run_6_3_TEST")
    assert reloaded.results[0].human_verdict is None
