"""Phase 5.3 — the Coach (spec §8).

The graded behaviour (plan 5.3): on a deliberately-broken dogfood project (the
``net_revenue`` measure removed), the Coach reads the failing dev question and proposes the
correct measure diff — under record-replay, spending no tokens. Alongside: it reads only
dev (refuses a test run), skips the LLM when dev is clean, ranks/parses proposals, and
persists a report for ``coach apply`` (Phase 5.4).
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import shutil
from pathlib import Path

import pytest

from sqbyl.coach import coach, gather_failures, latest_report, load_reports, save_report
from sqbyl.models import CoachLayer, QuestionResult, ScoredRun, Verdict
from sqbyl.project import Project
from sqbyl.yamlio import dump_yaml, load_yaml
from sqbyl_runtime.llm.mock import MockLLMClient, structured_reply
from sqbyl_runtime.llm.replay import RecordReplayLLMClient
from sqbyl_runtime.state.layout import SqbylPaths

_CASSETTE = Path(__file__).resolve().parent / "cassettes" / "coach_measure.json"

# The proposal the Coach returns for the missing-measure failure. Scripted for the mock and
# captured into the cassette — the test proves the *pipeline* surfaces it, not the model's
# reasoning (invariant 4).
_PROPOSAL = {
    "proposals": [
        {
            "title": "Add measure net_revenue to semantics/orders.yaml",
            "root_cause": "the agent summed amount_cents without excluding refunds",
            "layer": "measure",
            "target_file": "semantics/orders.yaml",
            "diff": (
                "--- a/semantics/orders.yaml\n+++ b/semantics/orders.yaml\n@@ measures @@\n"
                "+measures:\n+  - name: net_revenue\n+    description: Revenue net of refunds.\n"
                "+    sql: \"SUM(CASE WHEN status='confirmed' THEN amount_cents ELSE 0 END)/100.0\""
            ),
            "rationale": "A reusable measure fixes every revenue question at the semantics layer.",
            "predicted_fixes": 1,
            "confidence": 0.9,
            "question_ids": ["q_net_revenue_all"],
            "conflicts": "",
        }
    ]
}


def _broken_project(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Project:
    """Dogfood with the ``net_revenue`` measure stripped from orders.yaml."""
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))
    dst = tmp_path / "proj"
    shutil.copytree(dogfood_dir, dst)
    orders = dst / "semantics" / "orders.yaml"
    data = load_yaml(orders.read_text())
    data.pop("measures", None)  # break it: no net_revenue measure
    orders.write_text(dump_yaml(data))
    return Project.load(dst)


def _failing_run() -> ScoredRun:
    """A dev run where the revenue question is a mismatch (agent summed without netting)."""
    return ScoredRun(
        run_id="run_coach_1",
        split="dev",
        results=[
            QuestionResult(
                id="q_total_orders",
                question="How many orders are there in total?",
                verdict=Verdict.correct,
                generated_sql="SELECT COUNT(*) FROM analytics.orders",
            ),
            QuestionResult(
                id="q_net_revenue_all",
                question="What is total net revenue?",
                verdict=Verdict.manual_review,
                plan="sum all amounts",
                generated_sql="SELECT SUM(amount_cents)/100.0 FROM analytics.orders",
                gold_sql="SELECT SUM(amount_cents)/100.0 FROM analytics.orders "
                "WHERE status='confirmed'",
            ),
        ],
    )


def test_gather_failures_honors_human_resolution() -> None:
    run = _failing_run()
    # Unresolved review row is a failure …
    assert [r.id for r in gather_failures(run)] == ["q_net_revenue_all"]
    # … but once a human confirms it correct, it's no longer coached.
    run.results[1].human_verdict = Verdict.correct
    assert gather_failures(run) == []


def test_coach_refuses_a_test_run(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _broken_project(tmp_path, dogfood_dir, duckdb_path, monkeypatch)
    test_run = _failing_run().model_copy(update={"split": "test"})
    with pytest.raises(ValueError, match="only runs on the dev set"):
        coach(project, test_run, llm=MockLLMClient([]), model="claude-x")


def test_coach_skips_the_llm_when_dev_is_clean(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _broken_project(tmp_path, dogfood_dir, duckdb_path, monkeypatch)
    clean = ScoredRun(
        run_id="r_clean",
        split="dev",
        results=[
            QuestionResult(id="q1", question="q?", verdict=Verdict.correct, generated_sql="X")
        ],
    )
    mock = MockLLMClient([])  # would raise if the Coach called it
    report = coach(project, clean, llm=mock, model="claude-x")
    assert mock.call_count == 0
    assert report.n_proposals == 0 and report.n_failures == 0


def test_coach_proposes_the_missing_measure(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _broken_project(tmp_path, dogfood_dir, duckdb_path, monkeypatch)
    mock = MockLLMClient([structured_reply(_PROPOSAL)])

    report = coach(project, _failing_run(), llm=mock, model="claude-x")

    assert report.n_failures == 1
    assert report.n_proposals == 1
    p = report.proposals[0]
    assert p.id  # slugged from the title
    assert p.layer is CoachLayer.measure  # the right layer — semantics, not prose
    assert p.target_file == "semantics/orders.yaml"
    assert "net_revenue" in p.diff
    assert p.predicted_fixes == 1
    assert "q_net_revenue_all" in p.question_ids
    # The Coach was shown the broken file (no measure) — confirm the prompt carried it.
    sent = mock.requests[0].messages[-1].content
    assert "semantics/orders.yaml" in sent and "What is total net revenue?" in sent


def test_unknown_layer_degrades_to_prose(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _broken_project(tmp_path, dogfood_dir, duckdb_path, monkeypatch)
    payload = {"proposals": [dict(_PROPOSAL["proposals"][0], layer="made_up_layer")]}
    report = coach(
        project, _failing_run(), llm=MockLLMClient([structured_reply(payload)]), model="claude-x"
    )
    # An unrecognized layer can't masquerade as high-leverage — it's treated as last-resort prose.
    assert report.proposals[0].layer is CoachLayer.instruction
    assert report.proposals[0].is_prose


def _proposal(title: str, layer: str, *, fixes: int, qids: list[str], target: str) -> dict:
    return {
        "title": title,
        "root_cause": "rc",
        "layer": layer,
        "target_file": target,
        "diff": f"+ {title}",
        "predicted_fixes": fixes,
        "confidence": 0.8,
        "question_ids": qids,
        "conflicts": "",
    }


def test_proposals_are_ranked_deterministically_not_by_model_order(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _broken_project(tmp_path, dogfood_dir, duckdb_path, monkeypatch)
    # Emitted worst-first: a prose rule, a single-question example, then a real measure.
    payload = {
        "proposals": [
            _proposal(
                "Add instruction", "instruction", fixes=1, qids=["a"], target="instructions.md"
            ),
            _proposal(
                "Add example for a", "example", fixes=1, qids=["a"], target="examples/x.yaml"
            ),
            _proposal(
                "Add measure", "measure", fixes=2, qids=["a", "b"], target="semantics/o.yaml"
            ),
        ]
    }
    report = coach(
        project, _failing_run(), llm=MockLLMClient([structured_reply(payload)]), model="claude-x"
    )
    # Re-sorted by leverage: the general measure first; the memorization-risk example and the
    # prose rule sink to the bottom (prose last of all).
    assert [p.layer for p in report.proposals] == [
        CoachLayer.measure,
        CoachLayer.example,
        CoachLayer.instruction,
    ]
    assert report.proposals[1].memorization_risk is True  # singleton example
    assert report.proposals[2].is_prose is True


def test_report_stamps_the_source_agent_model(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _broken_project(tmp_path, dogfood_dir, duckdb_path, monkeypatch)
    run = _failing_run().model_copy(update={"models": {"agent": "claude-agent-9"}})
    report = coach(
        project, run, llm=MockLLMClient([structured_reply(_PROPOSAL)]), model="claude-coach"
    )
    # A proposal is only meaningful against the agent version that produced the failures.
    assert report.model == "claude-coach"
    assert report.source_models == {"agent": "claude-agent-9"}


def test_unresolved_mismatch_is_flagged_to_the_coach(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _broken_project(tmp_path, dogfood_dir, duckdb_path, monkeypatch)
    run = _failing_run()
    run.results[1].judge_suggestion = Verdict.correct  # judge thinks the agent SQL is equivalent
    mock = MockLLMClient([structured_reply(_PROPOSAL)])
    coach(project, run, llm=mock, model="claude-x")
    sent = mock.requests[0].messages[-1].content
    # The Coach is warned this row may be a false failure (don't force a context edit).
    assert "UNRESOLVED" in sent and "EQUIVALENT" in sent


def test_report_persists_and_reloads(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _broken_project(tmp_path, dogfood_dir, duckdb_path, monkeypatch)
    report = coach(
        project, _failing_run(), llm=MockLLMClient([structured_reply(_PROPOSAL)]), model="claude-x"
    )
    paths = SqbylPaths(project.root)
    save_report(paths, report)
    assert len(load_reports(paths)) == 1
    reloaded = latest_report(paths)
    assert reloaded is not None
    assert reloaded.run_id == report.run_id
    assert reloaded.proposals[0].target_file == "semantics/orders.yaml"


def _write_cassette(project: Project) -> None:
    capture = MockLLMClient([structured_reply(_PROPOSAL)])
    coach(project, _failing_run(), llm=capture, model="claude-x")
    entries = {
        req.fingerprint(): {
            "request": req.model_dump(mode="json"),
            "response": resp.model_dump(mode="json"),
        }
        for req, resp in zip(capture.requests, capture.responses, strict=True)
    }
    _CASSETTE.parent.mkdir(parents=True, exist_ok=True)
    _CASSETTE.write_text(
        json.dumps({"version": 1, "entries": entries}, indent=2, sort_keys=True) + "\n"
    )


def test_coach_replays_from_cassette(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _broken_project(tmp_path, dogfood_dir, duckdb_path, monkeypatch)
    if os.environ.get("SQBYL_UPDATE_CASSETTES") or not _CASSETTE.exists():
        _write_cassette(project)

    client = RecordReplayLLMClient(_CASSETTE, mode="replay")
    report = coach(project, _failing_run(), llm=client, model="claude-x")

    assert report.n_proposals == 1
    assert report.proposals[0].layer is CoachLayer.measure
    assert "net_revenue" in report.proposals[0].diff


def test_cli_coach_reads_the_dev_run_meters_and_persists(
    tmp_path: Path,
    dogfood_dir: Path,
    duckdb_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sqbyl.cli import main
    from sqbyl.eval.report import save_run
    from sqbyl_runtime.state.usage import UsageStore

    project = _broken_project(tmp_path, dogfood_dir, duckdb_path, monkeypatch)
    paths = SqbylPaths(project.root).ensure()
    save_run(paths, _failing_run())  # the dev run `coach` will read
    monkeypatch.setattr(
        "sqbyl.llm.build_llm_client", lambda *a, **k: MockLLMClient([structured_reply(_PROPOSAL)])
    )

    code = main(["coach", str(project.root)])

    assert code == 0
    out = capsys.readouterr().out
    assert "sqbyl Coach" in out and "net_revenue" in out  # the ranked proposal + its diff
    assert latest_report(paths) is not None  # persisted for `coach apply` (Phase 5.4)
    with UsageStore(paths.usage_db) as store:  # the paid call was metered as role=coach
        assert any(r.role == "coach" for r in store.all())


def test_cli_coach_nudges_when_no_dev_run(
    tmp_path: Path,
    dogfood_dir: Path,
    duckdb_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sqbyl.cli import main

    project = _broken_project(tmp_path, dogfood_dir, duckdb_path, monkeypatch)
    code = main(["coach", str(project.root)])
    assert code == 1
    assert "run `sqbyl eval dev` first" in capsys.readouterr().out


def test_coach_module_does_not_import_the_held_out_door() -> None:
    # The dev loop (coach) must not import `sqbyl.eval.heldout` — the import-linter contract
    # asserted at the AST level too (docstrings may name the module; only real imports fail).
    import sqbyl.coach

    tree = ast.parse(inspect.getsource(sqbyl.coach))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert "sqbyl.eval.heldout" not in imported
