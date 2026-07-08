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
from sqbyl.models import CoachEdit, CoachLayer, CoachProposal, QuestionResult, ScoredRun, Verdict
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
            "edits": [
                {
                    "find": "",  # append the measures block the broken file is missing
                    "replace": (
                        "measures:\n  - name: net_revenue\n"
                        "    description: Revenue net of refunds.\n"
                        "    sql: \"SUM(CASE WHEN status='confirmed' THEN amount_cents "
                        'ELSE 0 END)/100.0"'
                    ),
                }
            ],
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
    assert "net_revenue" in p.render_diff()
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
    # An unrecognized layer can't masquerade as high-leverage — its *layer* degrades to the
    # last-resort `instruction`, so LAYER_PREFERENCE ranks it dead last.
    assert report.proposals[0].layer is CoachLayer.instruction
    # ...but `is_prose` tracks the target file, not the layer: this edit writes a semantics yaml,
    # so it is NOT flagged "global prose — last resort" and NOT force-routed to human review.
    assert report.proposals[0].is_prose is False


def test_is_prose_tracks_the_target_file_not_the_self_reported_layer() -> None:
    # The bug (UX finding): the Coach mislabeled a well-targeted structured column edit as
    # layer=instruction. Trusting the layer stamped it with the "last resort" flag a reviewer
    # skips. is_prose must derive from where the edit actually writes.
    def mk(layer: CoachLayer, target: str) -> CoachProposal:
        return CoachProposal(id="p", title="t", root_cause="rc", layer=layer, target_file=target)

    assert mk(CoachLayer.instruction, "semantics/products.yaml").is_prose is False
    assert mk(CoachLayer.instruction, "./instructions.md").is_prose is True
    # And the reverse mislabel: a real prose edit the model tagged as a column change is prose.
    assert mk(CoachLayer.column_description, "instructions.md").is_prose is True


def _proposal(title: str, layer: str, *, fixes: int, qids: list[str], target: str) -> dict:
    return {
        "title": title,
        "root_cause": "rc",
        "layer": layer,
        "target_file": target,
        "edits": [{"find": "", "replace": title}],
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
    assert "net_revenue" in report.proposals[0].render_diff()


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


def _measure_proposal() -> CoachProposal:
    from sqbyl.models import CoachLayer, CoachProposal

    return CoachProposal(
        id="p1",
        title="Add net_revenue",
        root_cause="rc",
        layer=CoachLayer.measure,
        target_file="semantics/orders.yaml",
        edits=[
            CoachEdit(
                find="",
                replace=(
                    "measures:\n  - name: net_revenue\n    description: Revenue net of refunds.\n"
                    "    sql: \"SUM(CASE WHEN status='confirmed' THEN amount_cents "
                    'ELSE 0 END)/100.0"'
                ),
            )
        ],
    )


def test_apply_appends_the_missing_measure(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqbyl.coach import apply_proposal

    project = _broken_project(tmp_path, dogfood_dir, duckdb_path, monkeypatch)
    orders = project.root / "semantics" / "orders.yaml"
    assert "net_revenue" not in orders.read_text()  # broken

    path = apply_proposal(project, _measure_proposal())

    assert path == orders.resolve()
    assert "net_revenue" in orders.read_text()  # the measure is now on disk
    # It parses as valid YAML with the measure present (a real, reviewable file change).
    assert any(m["name"] == "net_revenue" for m in load_yaml(orders.read_text())["measures"])


def test_apply_creates_a_new_file(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqbyl.coach import apply_proposal
    from sqbyl.models import CoachLayer, CoachProposal

    project = _broken_project(tmp_path, dogfood_dir, duckdb_path, monkeypatch)
    proposal = CoachProposal(
        id="p",
        title="t",
        root_cause="rc",
        layer=CoachLayer.example,
        target_file="examples/growth.yaml",
        edits=[CoachEdit(find="", replace="- q: x\n  sql: Y")],
    )
    apply_proposal(project, proposal)
    assert (project.root / "examples" / "growth.yaml").read_text().startswith("- q: x")


def test_apply_refuses_ambiguous_or_missing_anchor(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqbyl.coach import ApplyError, apply_proposal
    from sqbyl.models import CoachLayer, CoachProposal

    project = _broken_project(tmp_path, dogfood_dir, duckdb_path, monkeypatch)

    def _edit(find: str) -> CoachProposal:
        return CoachProposal(
            id="p",
            title="t",
            root_cause="rc",
            layer=CoachLayer.measure,
            target_file="semantics/orders.yaml",
            edits=[CoachEdit(find=find, replace="X")],
        )

    with pytest.raises(ApplyError, match="not found"):
        apply_proposal(project, _edit("this text is nowhere in the file"))
    with pytest.raises(ApplyError, match="ambiguous"):
        apply_proposal(project, _edit("name"))  # 'name' appears many times


def test_apply_refuses_benchmarks_and_path_escapes(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqbyl.coach import ApplyError, apply_proposal
    from sqbyl.models import CoachLayer, CoachProposal

    project = _broken_project(tmp_path, dogfood_dir, duckdb_path, monkeypatch)

    def _target(target: str) -> CoachProposal:
        return CoachProposal(
            id="p",
            title="t",
            root_cause="rc",
            layer=CoachLayer.example,
            target_file=target,
            edits=[CoachEdit(find="", replace="x")],
        )

    # Allowlist, not a benchmarks denylist: the Coach may write ONLY its context files.
    with pytest.raises(ApplyError, match="benchmarks"):
        apply_proposal(project, _target("benchmarks/test.yaml"))  # held-out set (invariant 3)
    with pytest.raises(ApplyError, match="outside the project"):
        apply_proposal(project, _target("../escape.yaml"))  # path escape
    with pytest.raises(ApplyError, match="only edits the agent's context"):
        apply_proposal(project, _target("sqbyl.yaml"))  # the manifest (DB config)
    with pytest.raises(ApplyError, match="only edits the agent's context"):
        apply_proposal(project, _target(".sqbyl/usage.db"))  # local state
    with pytest.raises(ApplyError, match="only edits the agent's context"):
        apply_proposal(project, _target("Benchmarks/test.yaml"))  # case-insensitive lookalike

    # Traversal out of a writable dir into benchmarks resolves+refuses (not a string match).
    with pytest.raises(ApplyError):
        apply_proposal(project, _target("semantics/../benchmarks/test.yaml"))
    # A symlink sitting *inside* a writable dir but pointing at the held-out set is refused,
    # because .resolve() follows it to benchmarks/ (which isn't in the allowlist).
    link = project.root / "semantics" / "sneaky.yaml"
    link.symlink_to(project.root / "benchmarks" / "test.yaml")
    with pytest.raises(ApplyError):
        apply_proposal(project, _target("semantics/sneaky.yaml"))

    # …but the legitimate context surface (including a top-level instructions.md) is writable.
    p = apply_proposal(project, _target("instructions.md"))
    assert p.name == "instructions.md"


def test_journey_eval_coach_apply_eval_flips_a_question_green(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The canonical Milestone-2 loop (plan 5.4): eval dev → coach → coach apply → eval dev,
    # and the targeted question flips from a mismatch to correct. Deterministic (judge off) so
    # the flip is a real correctness change, not a judge opinion.
    from sqbyl.coach import apply_proposal, coach
    from sqbyl.eval.benchmarks_io import Split, load_dev_set
    from sqbyl.eval.runner import run_eval

    project = _broken_project(tmp_path, dogfood_dir, duckdb_path, monkeypatch)
    gold_by_q = {q.question: q.gold_sql or "" for q in load_dev_set(project)}
    revenue_q = "What is total net revenue?"

    def agent(request: object) -> object:
        req = request  # typing: MockLLMClient passes an LLMRequest
        system = req.system or ""  # type: ignore[attr-defined]
        text = req.messages[-1].content  # type: ignore[attr-defined]
        for question, gold in gold_by_q.items():
            if question in text:
                # The revenue answer is only correct once the agent's context has the MEASURE
                # (the compiler renders it as "measure net_revenue: ..."); everything else the
                # agent already gets right.
                if question == revenue_q and "measure net_revenue" not in system:
                    sql = "SELECT SUM(amount_cents)/100.0 FROM analytics.orders"  # includes refunds
                else:
                    sql = gold
                return structured_reply({"plan": "", "sql": sql, "used_assets": []})
        raise AssertionError(f"no scripted question matched: {text[:60]!r}")

    # 1) eval dev — the revenue question is a mismatch (agent summed refunds in too).
    run1 = run_eval(project, split=Split.dev, llm=MockLLMClient([agent] * 100), judge=False)
    before = {r.id: r.verdict for r in run1.results}
    assert before["q_net_revenue_all"] is Verdict.manual_review

    # 2) coach → 3) apply the measure fix to the real file.
    report = coach(project, run1, llm=MockLLMClient([structured_reply(_PROPOSAL)]), model="x")
    apply_proposal(project, report.proposals[0])
    assert "net_revenue" in (project.root / "semantics" / "orders.yaml").read_text()

    # 4) eval dev again — the agent now has the measure in context, so it flips green.
    run2 = run_eval(project, split=Split.dev, llm=MockLLMClient([agent] * 100), judge=False)
    after = {r.id: r.verdict for r in run2.results}
    assert after["q_net_revenue_all"] is Verdict.correct  # the targeted question flipped
    assert run2.accuracy > run1.accuracy  # and the dev score genuinely rose


def test_apply_refuses_a_drifted_file_unless_forced(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqbyl.coach import ApplyError, apply_proposal

    project = _broken_project(tmp_path, dogfood_dir, duckdb_path, monkeypatch)
    # coach() stamps the target file's fingerprint into the proposal.
    report = coach(
        project, _failing_run(), llm=MockLLMClient([structured_reply(_PROPOSAL)]), model="x"
    )
    proposal = report.proposals[0]
    assert proposal.target_fingerprint  # non-empty: orders.yaml existed when coached

    # The file changes out from under the proposal → apply refuses (a stale append is unsafe).
    orders = project.root / "semantics" / "orders.yaml"
    orders.write_text(orders.read_text() + "\n# an unrelated human edit\n")
    with pytest.raises(ApplyError, match="changed since the Coach saw it"):
        apply_proposal(project, proposal)
    # …but --force applies anyway (the operator's escape hatch).
    apply_proposal(project, proposal, force=True)
    assert "net_revenue" in orders.read_text()


def test_cli_apply_is_idempotent_no_double_write(
    tmp_path: Path,
    dogfood_dir: Path,
    duckdb_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sqbyl.cli import main
    from sqbyl.coach import save_report

    project = _broken_project(tmp_path, dogfood_dir, duckdb_path, monkeypatch)
    paths = SqbylPaths(project.root)
    report = coach(
        project, _failing_run(), llm=MockLLMClient([structured_reply(_PROPOSAL)]), model="x"
    )
    save_report(paths, report)
    orders = project.root / "semantics" / "orders.yaml"

    assert main(["coach", "apply", "1", str(project.root)]) == 0
    assert orders.read_text().count("name: net_revenue") == 1  # applied once

    # Re-applying the same proposal must NOT append the measure a second time (empty-`find`
    # append would silently duplicate) — the persisted applied-marker skips it.
    capsys.readouterr()
    assert main(["coach", "apply", "1", str(project.root)]) == 0
    out = capsys.readouterr().out
    assert "already applied" in out
    assert orders.read_text().count("name: net_revenue") == 1  # still exactly one


def test_cli_eval_test_surfaces_the_overfitting_gap(
    tmp_path: Path,
    dogfood_dir: Path,
    duckdb_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sqbyl.cli import main
    from sqbyl.eval.benchmarks_io import Split
    from sqbyl.eval.heldout import load_for_eval
    from sqbyl.eval.report import save_run
    from sqbyl.project import Project

    project = _broken_project(tmp_path, dogfood_dir, duckdb_path, monkeypatch)
    paths = SqbylPaths(project.root).ensure()
    # A perfect dev run on record …
    dev_qs = load_for_eval(project, Split.dev)
    save_run(
        paths,
        ScoredRun(
            run_id="dev_perfect",
            split="dev",
            results=[
                QuestionResult(
                    id=q.id, question=q.question, verdict=Verdict.correct, generated_sql="X"
                )
                for q in dev_qs
            ],
        ),
    )
    # … while the held-out test scores poorly (the agent answers every test question wrong).
    monkeypatch.setattr(
        "sqbyl.llm.build_llm_client",
        lambda *a, **k: MockLLMClient(
            [structured_reply({"plan": "", "sql": "SELECT 1", "used_assets": []})] * 100
        ),
    )
    project.manifest.automation.auto_judge = False  # isolate the deterministic gap
    monkeypatch.setattr(Project, "load", staticmethod(lambda *a, **k: project))

    main(["eval", "test", str(project.root)])
    out = capsys.readouterr().out
    assert "dev↔test gap" in out
    assert "overfitting" in out  # dev far above test → the warning fires


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
