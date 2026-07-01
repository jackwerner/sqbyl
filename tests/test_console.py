"""Phase 4.2 — the review console. Driven with Starlette's in-process TestClient.

The graded behaviour (plan 4.2): a synthesized candidate accepted in the UI appears in
``benchmarks/dev.yaml`` on disk. Alongside it: reject, edit-and-re-run-live, idempotent
re-accept, 404s, and the invariant-3 guarantee that the console only ever writes the dev
split — the held-out ``test.yaml`` is never touched.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from sqbyl.candidates_io import add_candidates, load_candidates
from sqbyl.console import create_app
from sqbyl.eval.benchmarks_io import Split, benchmark_path, load_dev_set
from sqbyl.models import Candidate, ExecutionEvidence
from sqbyl.project import Project

if TYPE_CHECKING:
    from starlette.testclient import TestClient


def _candidate(cid: str, question: str, gold_sql: str, **kw: object) -> Candidate:
    return Candidate(
        id=cid,
        question=question,
        gold_sql=gold_sql,
        evidence=ExecutionEvidence(columns=["n"], rows=[[1]], row_count=1),
        **kw,  # type: ignore[arg-type]
    )


@pytest.fixture
def project(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Project:
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))
    dst = tmp_path / "proj"
    shutil.copytree(dogfood_dir, dst)
    project = Project.load(dst)
    add_candidates(
        project,
        [
            _candidate(
                "q_cust_count", "How many customers?", "SELECT COUNT(*) FROM analytics.customers"
            ),
            _candidate(
                "q_orders_count",
                "How many orders?",
                "SELECT COUNT(*) FROM analytics.orders",
                difficulty="easy",
            ),
        ],
    )
    return project


@pytest.fixture
def client(project: Project) -> TestClient:
    # Imported here (not at module top) so Starlette's httpx-backend deprecation warning
    # fires during the test run, where pytest's filterwarnings suppresses it — not at
    # collection time, where it would leak into the summary.
    from starlette.testclient import TestClient

    return TestClient(create_app(project))


def test_index_serves_the_bundled_ui(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "sqbyl review" in resp.text  # the bundled single-page app


def test_list_returns_pending_candidates_with_counts(client: TestClient) -> None:
    data = client.get("/api/candidates").json()
    assert len(data["candidates"]) == 2
    assert data["counts"] == {"pending": 2, "accepted": 0, "rejected": 0}


def test_accept_appends_the_candidate_to_dev_yaml(project: Project, client: TestClient) -> None:
    before = {q.id for q in load_dev_set(project)}
    resp = client.post("/api/candidates/q_cust_count/accept", json={})
    body = resp.json()
    assert body["added_to_dev"] is True
    assert body["candidate"]["status"] == "accepted"

    after = load_dev_set(project)
    added = next(q for q in after if q.id == "q_cust_count")
    assert added.id not in before  # genuinely new
    assert added.gold_sql.startswith("SELECT COUNT(*)")


def test_accept_with_edits_writes_the_edited_question(project: Project, client: TestClient) -> None:
    edited_sql = "SELECT COUNT(DISTINCT customer_id) FROM analytics.customers"
    client.post(
        "/api/candidates/q_cust_count/accept",
        json={"question": "How many distinct customers?", "gold_sql": edited_sql},
    )
    added = next(q for q in load_dev_set(project) if q.id == "q_cust_count")
    assert added.question == "How many distinct customers?"
    assert added.gold_sql == edited_sql  # the edit, not the original


def test_accept_refuses_an_edit_that_no_longer_runs(project: Project, client: TestClient) -> None:
    # Re-grounding on accept (spec §6.A): an edited gold SQL that errors must NOT enter the
    # golden set — the console re-executes it and refuses.
    before = {q.id for q in load_dev_set(project)}
    resp = client.post(
        "/api/candidates/q_cust_count/accept",
        json={"gold_sql": "SELECT nonexistent FROM analytics.customers"},
    )
    body = resp.json()
    assert body["ok"] is False
    assert body["reason"] == "syntax_error"
    assert {q.id for q in load_dev_set(project)} == before  # nothing admitted
    # The candidate is still pending, so the reviewer can fix it.
    assert client.get("/api/candidates").json()["counts"]["pending"] == 2


def test_reaccepting_is_idempotent(project: Project, client: TestClient) -> None:
    assert (
        client.post("/api/candidates/q_cust_count/accept", json={}).json()["added_to_dev"] is True
    )
    # Second accept must not duplicate the row in dev.yaml.
    assert (
        client.post("/api/candidates/q_cust_count/accept", json={}).json()["added_to_dev"] is False
    )
    ids = [q.id for q in load_dev_set(project)]
    assert ids.count("q_cust_count") == 1


def test_reject_marks_without_writing_dev(project: Project, client: TestClient) -> None:
    before = {q.id for q in load_dev_set(project)}
    resp = client.post("/api/candidates/q_orders_count/reject", json={})
    assert resp.json()["candidate"]["status"] == "rejected"
    assert {q.id for q in load_dev_set(project)} == before  # nothing written
    assert client.get("/api/candidates").json()["counts"]["rejected"] == 1


def test_rerun_executes_edited_sql_live(client: TestClient) -> None:
    good = client.post(
        "/api/candidates/q_cust_count/rerun",
        json={"gold_sql": "SELECT COUNT(*) AS n FROM analytics.customers"},
    ).json()
    assert good["ok"] is True
    assert good["evidence"]["row_count"] == 1

    bad = client.post(
        "/api/candidates/q_cust_count/rerun",
        json={"gold_sql": "SELECT missing FROM analytics.customers"},
    ).json()
    assert bad["ok"] is False
    assert bad["reason"] == "syntax_error"


def test_unknown_candidate_is_404(client: TestClient) -> None:
    assert client.post("/api/candidates/nope/accept", json={}).status_code == 404
    assert client.post("/api/candidates/nope/reject", json={}).status_code == 404


def test_console_never_touches_the_held_out_set(project: Project, client: TestClient) -> None:
    test_before = benchmark_path(project, Split.test).read_text()
    client.post("/api/candidates/q_cust_count/accept", json={})
    client.post("/api/candidates/q_orders_count/reject", json={})
    assert benchmark_path(project, Split.test).read_text() == test_before  # invariant 3


def test_candidate_status_persists_in_the_queue(project: Project, client: TestClient) -> None:
    client.post("/api/candidates/q_cust_count/accept", json={})
    statuses = {c.id: c.status.value for c in load_candidates(project)}
    assert statuses["q_cust_count"] == "accepted"
    assert statuses["q_orders_count"] == "pending"
