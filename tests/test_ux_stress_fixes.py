"""Fresh-user UX stress-test fixes (v0.1.1 notes).

Covers the eight findings from a first-time-user setup pass — the four Coach/`init`
correctness bugs and the four missing affordances — each exercised mock-first / $0 so CI
never spends a token (invariant 4).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sqbyl import init as initmod
from sqbyl.cli import main
from sqbyl.coach import ApplyError, apply_proposal, coach, save_report
from sqbyl.models import (
    CoachEdit,
    CoachLayer,
    CoachProposal,
    JudgeVerdict,
    QuestionResult,
    ScoredRun,
    ScorerResult,
    Verdict,
)
from sqbyl.project import Project
from sqbyl_runtime.llm.mock import MockLLMClient, structured_reply
from sqbyl_runtime.state.layout import SqbylPaths

# ── shared fixtures ──────────────────────────────────────────────────────────────────────


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def mini_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Project:
    """A minimal, DB-free project with two hand-authored semantics files — enough for the
    Coach to render its prompt and validate proposals against real ``TableSemantics`` files.

    ``customers.yaml`` owns the anchor text (a ``total spending`` synonym on
    ``lifetime_value``) that the Coach — in the reported bug — mis-attributed to
    ``orders.yaml``."""
    monkeypatch.setenv("DATABASE_URL", "duckdb:///:memory:")
    root = tmp_path / "proj"
    _write(
        root / "sqbyl.yaml",
        "name: mini\n"
        "database:\n  dialect: duckdb\n  url: env:DATABASE_URL\n"
        "model:\n  provider: anthropic\n  api_key: env:ANTHROPIC_API_KEY\n",
    )
    _write(
        root / "semantics" / "orders.yaml",
        "table: orders\ncolumns:\n  - name: amount_cents\n    type: integer\n",
    )
    _write(
        root / "semantics" / "customers.yaml",
        "table: customers\n"
        "columns:\n"
        "  - name: id\n    type: integer\n"
        "  - name: lifetime_value\n"
        "    type: double\n"
        "    synonyms:\n"
        "      - total spending\n",
    )
    return Project.load(root)


def _failing_run() -> ScoredRun:
    """A dev run with one unresolved mismatch — so ``gather_failures`` is non-empty and the
    Coach actually makes its one paid (mocked) call."""
    return ScoredRun(
        run_id="run_ux_1",
        split="dev",
        results=[
            QuestionResult(
                id="q_total_spending",
                question="What is each customer's total spending?",
                verdict=Verdict.manual_review,
                generated_sql="SELECT lifetime_value FROM customers",
                gold_sql="SELECT SUM(quantity * unit_price) FROM order_items GROUP BY customer_id",
            )
        ],
    )


def _coach_reply(target_file: str, edits: list[dict[str, str]]) -> MockLLMClient:
    return MockLLMClient(
        [
            structured_reply(
                {
                    "proposals": [
                        {
                            "title": "Fix total-spending confusion",
                            "root_cause": "the agent used lifetime_value instead of summing items",
                            "layer": "synonym",
                            "target_file": target_file,
                            "edits": edits,
                            "predicted_fixes": 1,
                            "confidence": 0.6,
                            "question_ids": ["q_total_spending"],
                        }
                    ]
                }
            )
        ]
    )


# ── #3: a mislocated anchor is relocated to the file that actually owns it ────────────────


def test_coach_relocates_a_mislocated_anchor(mini_project: Project) -> None:
    # The anchor lives in customers.yaml, but the model named orders.yaml (the reported bug).
    llm = _coach_reply(
        "semantics/orders.yaml",
        [{"find": "      - total spending", "replace": "      - total spending\n      - spend"}],
    )
    report = coach(mini_project, _failing_run(), llm=llm, model="claude-opus-4-8")
    assert report.n_proposals == 1
    p = report.proposals[0]
    assert p.target_file == "semantics/customers.yaml"  # corrected to the real owner
    assert p.edits, "the (now-relocatable) edit is kept"
    # And it now applies cleanly to the right file, leaving it loadable.
    apply_proposal(mini_project, p)
    assert Project.load(mini_project.root)  # customers.yaml still parses


# ── #4a/#4d: an edit that would introduce an unknown field is dropped at generation time ──


def test_coach_drops_a_schema_breaking_edit(mini_project: Project) -> None:
    # `description_note` is not a Column field (extra_forbid) — the edit must be stripped so it
    # can never be "applied" into a file that then fails to load on the next command.
    llm = _coach_reply(
        "semantics/customers.yaml",
        [{"find": "    type: double", "replace": "    type: double\n    description_note: x"}],
    )
    report = coach(mini_project, _failing_run(), llm=llm, model="claude-opus-4-8")
    p = report.proposals[0]
    assert p.edits == []  # the invalid edit was dropped; nothing to apply


# ── #4c: an edit-less proposal refuses to apply (no false "✓ applied") ────────────────────


def test_apply_refuses_an_edit_less_proposal(mini_project: Project) -> None:
    proposal = CoachProposal(
        id="p",
        title="prose only",
        root_cause="rc",
        layer=CoachLayer.synonym,
        target_file="semantics/customers.yaml",
        edits=[],
    )
    with pytest.raises(ApplyError, match="no edits to apply"):
        apply_proposal(mini_project, proposal)


# ── #4b: apply never leaves a semantics file that fails its schema ────────────────────────


def test_apply_rejects_a_field_that_breaks_schema(mini_project: Project) -> None:
    customers = mini_project.root / "semantics" / "customers.yaml"
    before = customers.read_text()
    bad = CoachProposal(
        id="p",
        title="invent a field",
        root_cause="rc",
        layer=CoachLayer.synonym,
        target_file="semantics/customers.yaml",
        edits=[CoachEdit(find="    type: double", replace="    type: double\n    bogus_key: 1")],
    )
    with pytest.raises(ApplyError, match="schema validation"):
        apply_proposal(mini_project, bad)
    assert customers.read_text() == before  # file untouched
    assert Project.load(mini_project.root)  # still loads


# ── #7b: `sqbyl coach` reuses an existing report for the current dev run ($0) ─────────────


def test_coach_reuses_existing_report_for_free(
    mini_project: Project, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqbyl.eval.report import save_run

    run = _failing_run()
    paths = SqbylPaths(mini_project.root).ensure()
    save_run(paths, run)
    report = coach(
        mini_project,
        run,
        llm=_coach_reply(
            "semantics/customers.yaml",
            [
                {
                    "find": "      - total spending",
                    "replace": "      - total spending\n      - spend",
                }
            ],
        ),
        model="claude-opus-4-8",
    )
    save_report(paths, report)

    # Re-running `coach` must NOT build a client (no re-spend) — it shows the saved report.
    monkeypatch.setattr(
        "sqbyl.llm.build_llm_client",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("reuse must not build a client")),
    )
    code = main(["coach", str(mini_project.root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "already exists" in out and "$0" in out
    assert "--regenerate" in out


# ── the `init` guided flow: scaffold, override, preflight (#1, #2, #5) ────────────────────


def test_init_writes_a_template_when_manifest_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No sqbyl.yaml and no TTY (pytest) → a ready-to-fill template is written, not a traceback.
    empty = tmp_path / "fresh"
    empty.mkdir()
    code = main(["init", str(empty)])
    assert code == 1
    out = capsys.readouterr().out
    assert "template" in out and "re-run" in out
    written = (empty / "sqbyl.yaml").read_text()
    assert "dialect:" in written and "api_key:" in written


def test_interactive_scaffold_writes_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqbyl.cli import _scaffold_interactive

    answers = iter(["demo", "duckdb", "env:DATABASE_URL", "anthropic", "ANTHROPIC_API_KEY"])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    project = _scaffold_interactive(tmp_path / "p")
    assert project is not None
    assert project.manifest.name == "demo"
    assert project.manifest.database.dialect.value == "duckdb"
    assert (tmp_path / "p" / "sqbyl.yaml").exists()


def test_build_manifest_yaml_is_loadable_and_wraps_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "duckdb:///:memory:")
    text = initmod.build_manifest_yaml(
        name="demo",
        dialect="duckdb",
        url="env:DATABASE_URL",
        provider="anthropic",
        api_key_var="ANTHROPIC_API_KEY",  # bare var → wrapped in env: indirection
    )
    assert "api_key: env:ANTHROPIC_API_KEY" in text
    path = initmod.write_manifest(tmp_path / "p", text)
    project = Project.load(path.parent)
    assert project.manifest.name == "demo"


def test_build_manifest_yaml_rejects_a_bad_dialect() -> None:
    with pytest.raises(Exception):  # noqa: B017 — pydantic raises ValidationError
        initmod.build_manifest_yaml(
            name="x", dialect="oracle", url="env:X", provider="anthropic", api_key_var="K"
        )


@pytest.fixture
def cold(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused")
    dst = tmp_path / "cold"
    shutil.copytree(dogfood_dir, dst)
    for stale in (dst / "semantics").glob("*.yaml"):
        stale.unlink()
    (dst / "benchmarks" / "dev.yaml").unlink()
    return dst


def test_model_override_reprices_synth_and_judge(cold: Path) -> None:
    haiku = "claude-haiku-4-5-20251001"
    project = Project.load(cold)
    free = initmod.run_free_pass(project)

    # The old bug: --model (as `model=`) moved annotate/eval but NOT synth/judge.
    partial = initmod.build_plan(project, free, model=haiku, synth_n=5)
    by_label = {i.label.split()[0]: i.model for i in partial.estimate.items}
    assert by_label["synthesize"] == "claude-opus-4-8"  # still pinned to default — the reported bug
    assert by_label["judge"] == "claude-opus-4-8"

    # The fix: a global override reprices every role (no per-role pins in the dogfood manifest).
    full = initmod.build_plan(project, free, model=haiku, synth_n=5, override=haiku)
    assert {i.model for i in full.estimate.items} == {haiku}
    assert full.override == haiku
    assert full.estimate.total_usd < partial.estimate.total_usd  # the swap is real, not partial


def test_for_role_override_respects_explicit_pins() -> None:
    from sqbyl.models import ModelConfig

    cfg = ModelConfig(api_key="env:K", default="d", synth_model="pinned-synth")
    assert cfg.for_role("synth", override="ovr") == "pinned-synth"  # explicit pin wins
    assert cfg.for_role("coach", override="ovr") == "ovr"  # else the override
    assert cfg.for_role("coach") == "d"  # else the default


def test_init_preflight_aborts_on_a_bad_credential(
    cold: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class _BadKeyClient:
        def check_auth(self) -> None:
            raise RuntimeError("credential check failed — verify ANTHROPIC_API_KEY")

    monkeypatch.setattr("sqbyl.llm.build_llm_client", lambda *a, **k: _BadKeyClient())
    code = main(["init", str(cold), "--auto", "--budget", "5"])
    assert code == 1
    assert "credential check failed" in capsys.readouterr().out
    # A failed preflight spends nothing.
    db = SqbylPaths(cold).usage_db
    assert not db.exists()


# ── read-only affordances: eval show + the split-aware empty hint (#6, #8) ────────────────


def _saved_run(paths: SqbylPaths) -> ScoredRun:
    from sqbyl.eval.report import save_run

    run = ScoredRun(
        run_id="run_show_1",
        split="dev",
        results=[
            QuestionResult(
                id="q1",
                question="How much revenue?",
                verdict=Verdict.manual_review,
                plan="sum amounts",
                generated_sql="SELECT SUM(amount_cents) FROM orders",
                gold_sql="SELECT SUM(amount_cents) FROM orders WHERE status='confirmed'",
                scorers=[ScorerResult(name="rowcount", passed=False, detail="1 vs 1 row")],
                judge_verdicts=[
                    JudgeVerdict(
                        judge="equivalence", passed=False, confidence=0.7, rationale="nets"
                    )
                ],
                judge_suggestion=Verdict.incorrect,
            )
        ],
    )
    save_run(paths, run)
    return run


def test_eval_show_prints_full_row_detail(
    mini_project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = SqbylPaths(mini_project.root).ensure()
    _saved_run(paths)
    code = main(["eval", "show", "dev", "q1", str(mini_project.root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "generated SQL" in out and "gold SQL" in out
    assert "rowcount" in out and "1 vs 1 row" in out  # scorer name + detail
    assert "equivalence" in out and "nets" in out  # judge verdict + rationale


def test_eval_show_reports_an_unknown_id(
    mini_project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = SqbylPaths(mini_project.root).ensure()
    _saved_run(paths)
    code = main(["eval", "show", "dev", "nope", str(mini_project.root)])
    assert code == 1
    assert "no question 'nope'" in capsys.readouterr().out


def test_empty_test_split_gives_hand_authored_hint(
    tmp_path: Path,
    dogfood_dir: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "duckdb:///:memory:")
    dst = tmp_path / "proj"
    shutil.copytree(dogfood_dir, dst)
    (dst / "benchmarks" / "test.yaml").write_text("[]\n")
    code = main(["eval", "test", str(dst)])
    assert code == 1
    out = capsys.readouterr().out
    assert "held-out" in out and "hand-authored" in out
    assert "run `sqbyl synth` first" not in out  # the old, categorically-wrong advice is gone
