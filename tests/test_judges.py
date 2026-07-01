"""Phase 5.1 — Layer-2 LLM judges + the arbiter (spec §7).

The graded behaviours (plan 5.1): a result-set mismatch routes to the judge panel; a
*passing* row provably skips the judges (zero LLM cost); and the arbiter only scores a row
when the panel is unanimous and confident, otherwise flagging manual-review. Unit tests
cover the fold rule and short-circuit directly; an end-to-end run proves the wiring and a
record-replay cassette pins it token-free.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sqbyl.eval.benchmarks_io import Split, load_dev_set
from sqbyl.eval.judges import (
    adjudicate,
    fold_panel,
    load_judge_prompt,
    load_judge_prompts,
    run_judge,
)
from sqbyl.eval.runner import run_eval
from sqbyl.models import (
    ALL_JUDGES,
    GOLD_MISMATCH_JUDGES,
    NO_GOLD_JUDGES,
    CalibrationRecord,
    JudgeVerdict,
    Verdict,
)
from sqbyl.models.benchmarks import BenchmarkQuestion
from sqbyl.project import Project
from sqbyl_runtime.llm.base import LLMRequest, LLMResponse
from sqbyl_runtime.llm.mock import MockLLMClient, structured_reply
from sqbyl_runtime.llm.replay import RecordReplayLLMClient
from sqbyl_runtime.models import Dialect

_CASSETTE = Path(__file__).resolve().parent / "cassettes" / "judge_dev.json"


@pytest.fixture(autouse=True)
def _fixture_db(duckdb_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))


def _v(judge: str, passed: bool, confidence: float) -> JudgeVerdict:
    return JudgeVerdict(judge=judge, passed=passed, confidence=confidence, rationale="because")


# --- the arbiter fold rule (spec §7): score only a unanimous, confident panel -----------


def test_fold_all_pass_confident_is_correct() -> None:
    panel = [_v(n, True, 0.9) for n in GOLD_MISMATCH_JUDGES]
    assert fold_panel(panel) is Verdict.correct


def test_fold_all_fail_confident_is_incorrect() -> None:
    panel = [_v(n, False, 0.9) for n in GOLD_MISMATCH_JUDGES]
    assert fold_panel(panel) is Verdict.incorrect


def test_fold_split_panel_stays_manual_review() -> None:
    # A dimension disagreeing (equivalent but incomplete) is not something to silently score.
    panel = [_v("semantic_equivalence", True, 0.9), _v("completeness", False, 0.9)]
    assert fold_panel(panel) is Verdict.manual_review


def test_fold_low_confidence_stays_manual_review() -> None:
    # Unanimous but the least-sure judge is below the bar → route to a human, don't score.
    panel = [_v(n, True, 0.9) for n in GOLD_MISMATCH_JUDGES]
    panel[-1] = _v(panel[-1].judge, True, 0.4)
    assert fold_panel(panel) is Verdict.manual_review


def test_fold_empty_panel_is_manual_review() -> None:
    assert fold_panel([]) is Verdict.manual_review


# --- one judge call --------------------------------------------------------------------


def test_run_judge_stamps_the_judge_name_and_uses_the_prompt() -> None:
    # The model even echoes the *wrong* name; run_judge overwrites it from the argument.
    mock = MockLLMClient(
        [structured_reply({"judge": "WRONG", "passed": True, "confidence": 0.8, "rationale": "ok"})]
    )
    verdict, usage = run_judge(
        mock,
        "semantic_equivalence",
        "JUDGE PROMPT TEXT",
        question="How many orders?",
        generated_sql="SELECT count(*) FROM analytics.orders",
        gold_sql="SELECT count(*) FROM analytics.orders",
        dialect=Dialect.duckdb,
        model="claude-x",
    )
    assert verdict.judge == "semantic_equivalence"  # authoritative, not the echoed value
    assert verdict.passed is True
    assert mock.requests[0].system == "JUDGE PROMPT TEXT"  # the editable prompt is the system
    assert usage.total_tokens > 0
    # With no calibration examples, the prompt carries no coaching block (so a fresh
    # project's judge requests — and the CI cassettes — are unchanged).
    assert "PRIOR HUMAN RULINGS" not in mock.requests[0].messages[-1].content


def test_run_judge_injects_prior_human_rulings_as_few_shot() -> None:
    # A human override becomes a calibration example replayed to the judge (spec §7).
    example = CalibrationRecord(
        run_id="r1",
        question_id="q1",
        judge_suggestion=Verdict.incorrect,
        human_verdict=Verdict.correct,
        agreed=False,
        question="How many paying customers?",
        generated_sql="SELECT count(*) FROM analytics.customers WHERE plan <> 'free'",
        gold_sql="SELECT count(*) FROM analytics.customers WHERE is_active",
        note="'paying' means not on the free plan here",
    )
    mock = MockLLMClient(
        [structured_reply({"judge": "x", "passed": True, "confidence": 0.9, "rationale": "ok"})]
    )
    run_judge(
        mock,
        "semantic_equivalence",
        "PROMPT",
        question="How many orders?",
        generated_sql="SELECT count(*) FROM analytics.orders",
        gold_sql="SELECT count(*) FROM analytics.orders",
        dialect=Dialect.duckdb,
        model="claude-x",
        examples=[example],
    )
    case = mock.requests[0].messages[-1].content
    assert "PRIOR HUMAN RULINGS" in case  # the coaching block is present
    assert "How many paying customers?" in case  # the example's question
    # Framed as the row's overall disposition, not a per-dimension label (avoids mis-coaching
    # a narrow-dimension judge).
    assert "final disposition of the row: correct" in case
    assert "not on the free plan" in case  # the human's note rides along


# --- the arbiter: which judges run, and the short-circuit ------------------------------


def _panel_reply(request: LLMRequest) -> LLMResponse:
    """A judge that passes every case (used to exercise panel wiring)."""
    return structured_reply(
        {"judge": "x", "passed": True, "confidence": 0.9, "rationale": "equivalent"}
    )


def test_adjudicate_skips_judges_on_a_correct_row() -> None:
    # The load-bearing guarantee (spec §7): a passing row costs zero tokens.
    mock = MockLLMClient([])  # exhausts loudly if any judge is called
    outcome = adjudicate(
        mock,
        verdict=Verdict.correct,
        question="q",
        generated_sql="SELECT 1",
        gold_sql="SELECT 1",
        prompts=dict.fromkeys(ALL_JUDGES, "p"),
        dialect=Dialect.duckdb,
        model="claude-x",
    )
    assert mock.call_count == 0
    assert outcome.suggestion is None  # Layer 1 stands; the advisory panel never ran


def test_adjudicate_runs_the_full_panel_when_a_gold_exists() -> None:
    mock = MockLLMClient([_panel_reply] * len(GOLD_MISMATCH_JUDGES))
    outcome = adjudicate(
        mock,
        verdict=Verdict.manual_review,
        question="q",
        generated_sql="SELECT 1",
        gold_sql="SELECT 1",
        prompts=dict.fromkeys(ALL_JUDGES, "p"),
        dialect=Dialect.duckdb,
        model="claude-x",
    )
    assert mock.call_count == len(GOLD_MISMATCH_JUDGES)
    assert [v.judge for v in outcome.judge_verdicts] == list(GOLD_MISMATCH_JUDGES)
    assert outcome.suggestion is Verdict.correct  # all passed, all confident → likely-equivalent
    assert outcome.usage.total_tokens > 0


def test_adjudicate_runs_the_gold_free_panel_when_there_is_no_gold() -> None:
    mock = MockLLMClient([_panel_reply] * len(NO_GOLD_JUDGES))
    outcome = adjudicate(
        mock,
        verdict=Verdict.manual_review,
        question="q",
        generated_sql="SELECT 1",
        gold_sql=None,
        prompts=dict.fromkeys(ALL_JUDGES, "p"),
        dialect=Dialect.duckdb,
        model="claude-x",
    )
    assert [v.judge for v in outcome.judge_verdicts] == list(NO_GOLD_JUDGES)
    assert "semantic_equivalence" not in {v.judge for v in outcome.judge_verdicts}


def test_adjudicate_survives_a_malformed_judge_response() -> None:
    # A judge returning off-schema JSON must not sink the run: it's dropped from the panel,
    # its tokens are still counted, and — with no verdicts left — the row stays reviewable.
    def bad(request: LLMRequest) -> LLMResponse:
        return structured_reply({"judge": "x"})  # missing required passed/confidence

    mock = MockLLMClient([bad] * len(GOLD_MISMATCH_JUDGES))
    outcome = adjudicate(
        mock,
        verdict=Verdict.manual_review,
        question="q",
        generated_sql="SELECT 1",
        gold_sql="SELECT 1",
        prompts=dict.fromkeys(ALL_JUDGES, "p"),
        dialect=Dialect.duckdb,
        model="claude-x",
    )
    assert mock.call_count == len(GOLD_MISMATCH_JUDGES)  # every judge was still called
    assert outcome.judge_verdicts == []  # none parsed
    assert outcome.suggestion is Verdict.manual_review  # degrades to "a human looks"
    assert outcome.usage.total_tokens > 0  # the calls are metered even though they failed


# --- editable prompts on disk ----------------------------------------------------------


def test_prompt_prefers_disk_over_default(tmp_path: Path, dogfood_dir: Path) -> None:
    import shutil

    dst = tmp_path / "proj"
    shutil.copytree(dogfood_dir, dst)
    (dst / "judges" / "completeness.md").write_text("MY TUNED COMPLETENESS PROMPT")
    project = Project.load(dst)
    assert load_judge_prompt(project, "completeness") == "MY TUNED COMPLETENESS PROMPT"


def test_prompt_falls_back_to_bundled_default(tmp_path: Path, dogfood_dir: Path) -> None:
    import shutil

    dst = tmp_path / "proj"
    shutil.copytree(dogfood_dir, dst)
    (dst / "judges").mkdir(exist_ok=True)
    # No file for this judge → the bundled default is used.
    (dst / "judges" / "completeness.md").unlink(missing_ok=True)
    project = Project.load(dst)
    prompt = load_judge_prompt(project, "completeness")
    assert "FULLY answers" in prompt or "fully answer" in prompt.lower()


def test_unknown_judge_name_raises(dogfood_dir: Path) -> None:
    project = Project.load(dogfood_dir)
    with pytest.raises(ValueError, match="unknown judge"):
        load_judge_prompt(project, "not_a_judge")


def test_load_judge_prompts_covers_every_judge(dogfood_dir: Path) -> None:
    project = Project.load(dogfood_dir)
    prompts = load_judge_prompts(project)
    assert set(prompts) == set(ALL_JUDGES)


# --- end-to-end through the runner -----------------------------------------------------


def _dispatch(gold_by_question: dict[str, str], *, wrong: str) -> object:
    """A single mock reply function that answers agent calls with gold (sabotaging one
    question) and answers every judge call with a confident PASS — so the sabotaged row's
    mismatch is adjudicated back to ``correct`` (the 'different SQL, same meaning' path)."""

    def reply(request: LLMRequest) -> LLMResponse:
        if (request.response_schema or {}).get("title") == "JudgeVerdict":
            return structured_reply(
                {"judge": "x", "passed": True, "confidence": 0.95, "rationale": "equivalent"}
            )
        text = request.messages[-1].content
        for question, gold in gold_by_question.items():
            if question in text:
                sql = "SELECT COUNT(*) + 1 FROM analytics.orders" if question == wrong else gold
                return structured_reply({"plan": "answer", "sql": sql, "used_assets": []})
        raise AssertionError(f"no scripted question matched: {text[:80]!r}")

    return reply


def test_judge_suggests_but_never_moves_the_headline(dogfood_dir: Path) -> None:
    # The core of the advisory design: a judged mismatch gets a *suggestion*, but the row
    # stays in the review pile and the deterministic accuracy is unchanged by the judge.
    project = Project.load(dogfood_dir)
    questions = load_dev_set(project)
    gold_by_q = {q.question: q.gold_sql or "" for q in questions}
    wrong = questions[0].question
    mock = MockLLMClient([_dispatch(gold_by_q, wrong=wrong)] * 100)

    run = run_eval(project, split=Split.dev, llm=mock, judge=True)

    first = run.results[0]
    assert first.verdict is Verdict.manual_review  # the deterministic verdict of record
    assert first.judge_suggestion is Verdict.correct  # advisory hint only ("likely-equivalent")
    assert first.judged and len(first.judge_verdicts) == len(GOLD_MISMATCH_JUDGES)
    # Headline accuracy is the deterministic floor — the sabotaged row does NOT count correct.
    assert run.accuracy == (len(questions) - 1) / len(questions)
    assert run.n_manual_review == 1
    assert run.n_suggested(Verdict.correct) == 1  # the review pile's triage breakdown
    assert run.models["judge"]  # the judge model is stamped on the run (spec §7)
    # Judge spend is metered separately from the agent's, not folded in.
    assert first.judge_usage.total_tokens > 0
    assert first.judge_cost_usd >= 0.0


def test_passing_rows_make_zero_judge_calls(dogfood_dir: Path) -> None:
    # The provable-skip guarantee at the run level: an all-correct run calls exactly one
    # agent completion per question and never a judge.
    project = Project.load(dogfood_dir)
    questions = load_dev_set(project)
    gold_by_q = {q.question: q.gold_sql or "" for q in questions}
    mock = MockLLMClient([_dispatch(gold_by_q, wrong="__none__")] * 100)

    run = run_eval(project, split=Split.dev, llm=mock, judge=True)

    assert run.accuracy == 1.0
    assert mock.call_count == len(questions)  # one agent call each, zero judge calls
    assert run.total_usage.total_tokens == sum(r.usage.total_tokens for r in run.results)


def _write_cassette(project: Project, questions: list[BenchmarkQuestion]) -> None:
    gold_by_q = {q.question: q.gold_sql or "" for q in questions}
    capture = MockLLMClient([_dispatch(gold_by_q, wrong=questions[0].question)] * 100)
    run_eval(project, split=Split.dev, llm=capture, judge=True)
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


def test_judged_run_replays_from_cassette(dogfood_dir: Path) -> None:
    project = Project.load(dogfood_dir)
    questions = load_dev_set(project)
    if os.environ.get("SQBYL_UPDATE_CASSETTES") or not _CASSETTE.exists():
        _write_cassette(project, questions)

    client = RecordReplayLLMClient(_CASSETTE, mode="replay")
    run = run_eval(project, split=Split.dev, llm=client, judge=True)

    # Deterministic accuracy is the floor (one row was sabotaged); the judge only advises.
    assert run.accuracy == (len(questions) - 1) / len(questions)
    assert run.results[0].judged  # the mismatch row went through the advisory judge panel
    assert run.results[0].judge_suggestion is Verdict.correct
