"""The review-console FastAPI app (spec §6.5, plan 4.2).

A thin, local surface over the project files. It reads the synth candidate queue
(``.sqbyl/candidates.yaml``), shows each candidate's question + gold SQL + the rows it
actually returned, and lets a human accept / edit / reject and re-run edited SQL live.
Accepting writes the (possibly edited) question to ``benchmarks/dev.yaml`` through the
dev-hard-wired :func:`~sqbyl.eval.benchmarks_io.append_to_dev_set` — so the console can
only ever grow the **dev** set, never the held-out ``test.yaml`` (invariant 3).

Endpoints are plain synchronous handlers (Starlette runs them in a threadpool); the DB is
opened per request since the console is a local, single-user tool.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from sqbyl.calibration_io import append_calibration, judge_agreement
from sqbyl.candidates_io import (
    get_candidate,
    load_candidates,
    update_candidate,
)
from sqbyl.eval.benchmarks_io import append_to_dev_set
from sqbyl.eval.report import latest_run, save_run
from sqbyl.models import (
    CalibrationRecord,
    Candidate,
    CandidateStatus,
    ExecutionEvidence,
    QuestionResult,
    ScoredRun,
    Verdict,
)
from sqbyl.project import Project
from sqbyl.synth import check_gold_sql
from sqbyl_runtime.state.layout import SqbylPaths

_STATIC = Path(__file__).resolve().parent / "static"

# The verdicts a human may assign in the review console. The judge's advisory suggestion is
# never authoritative; only these human calls resolve a row (spec §7).
_HUMAN_VERDICTS = {Verdict.correct, Verdict.incorrect, Verdict.manual_review}


class Edit(BaseModel):
    """Optional per-field overrides supplied on accept / re-run (``None`` = keep as-is)."""

    question: str | None = None
    gold_sql: str | None = None
    difficulty: str | None = None
    canonical: bool | None = None


class RerunRequest(BaseModel):
    gold_sql: str


class Resolve(BaseModel):
    """A human's authoritative call on a judged row (spec §7). ``verdict`` is the resolution
    of record; ``split`` picks which split's latest run to resolve against (default dev);
    ``note`` is an optional reason that rides along into the judge's few-shot examples."""

    verdict: Verdict
    split: str = "dev"
    note: str = ""


def _apply_edit(candidate: Candidate, edit: Edit) -> Candidate:
    updates = {k: v for k, v in edit.model_dump().items() if v is not None}
    return candidate.model_copy(update=updates) if updates else candidate


def _review_row(r: QuestionResult) -> dict[str, object]:
    """One row for the review surface: what the agent produced, what the judge advised (with
    each judge's rationale — *why* it needs review), and the human's call if made."""
    return {
        "id": r.id,
        "question": r.question,
        "generated_sql": r.generated_sql,
        "gold_sql": r.gold_sql,
        "gold_asset": r.gold_asset,
        "judge_suggestion": r.judge_suggestion.value if r.judge_suggestion else None,
        "judge_verdicts": [j.model_dump(mode="json") for j in r.judge_verdicts],
        "human_verdict": r.human_verdict.value if r.human_verdict else None,
        "resolved_verdict": r.resolved_verdict.value,
        "reviewed": r.reviewed,
    }


def _headline(run: ScoredRun, project: Project) -> dict[str, object]:
    """The numbers that sit atop the review view: the deterministic floor, the human-trusted
    resolved accuracy (which flips as overrides land), the work left, and — the point of it
    all — the live judge↔human agreement (scoped to this run's split, selection-biased)."""
    agreement = judge_agreement(project, split=run.split)
    return {
        "run_id": run.run_id,
        "split": run.split,
        "total": run.total,
        "accuracy": run.accuracy,  # deterministic floor — the judge never moves this
        "resolved_accuracy": run.resolved_accuracy,  # human-trusted; climbs on override
        "n_unreviewed": run.n_unreviewed,
        "n_reviewed": run.n_reviewed,
        "agreement": {"n": agreement.n, "n_agree": agreement.n_agree, "rate": agreement.rate},
    }


def create_app(project: Project) -> FastAPI:
    """Build the review console bound to one loaded ``project``."""
    app = FastAPI(title="sqbyl review", docs_url=None, redoc_url=None)
    dialect = project.manifest.database.dialect
    paths = SqbylPaths(project.root)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (_STATIC / "index.html").read_text()

    @app.get("/api/candidates")
    def list_candidates() -> dict[str, object]:
        candidates = load_candidates(project)
        return {
            "candidates": [c.model_dump(mode="json") for c in candidates],
            "counts": {
                "pending": sum(c.status is CandidateStatus.pending for c in candidates),
                "accepted": sum(c.status is CandidateStatus.accepted for c in candidates),
                "rejected": sum(c.status is CandidateStatus.rejected for c in candidates),
            },
        }

    @app.post("/api/candidates/{candidate_id}/rerun")
    def rerun(candidate_id: str, req: RerunRequest) -> dict[str, object]:
        # Edit-and-re-run-live: execute the edited SQL and report rows or the error, so a
        # reviewer can verify a fix before accepting. Read-only; nothing is written.
        _require(project, candidate_id)
        with project.connect() as db:
            evidence, reason, detail = check_gold_sql(db, req.gold_sql, dialect=dialect)
        if evidence is None:
            return {"ok": False, "reason": reason.value if reason else "error", "detail": detail}
        return {"ok": True, "evidence": evidence.model_dump(mode="json")}

    @app.post("/api/candidates/{candidate_id}/accept")
    def accept(candidate_id: str, edit: Edit) -> dict[str, object]:
        candidate = _require(project, candidate_id)
        edited = _apply_edit(candidate, edit)
        # Re-ground before admitting to the golden set — the whole premise is that only
        # questions whose gold SQL *runs* enter the benchmark (spec §6.A). An edit (or a
        # since-changed database) could make it error / go empty / degenerate, so we execute
        # it fresh and refuse the accept if it no longer produces a real answer. This also
        # refreshes the stored evidence to exactly what ran.
        with project.connect() as db:
            evidence, reason, detail = check_gold_sql(db, edited.gold_sql, dialect=dialect)
        if evidence is None:
            return {"ok": False, "reason": reason.value if reason else "error", "detail": detail}
        edited = edited.model_copy(update={"evidence": evidence})
        added = append_to_dev_set(project, [edited.to_question()])
        accepted = edited.model_copy(update={"status": CandidateStatus.accepted})
        update_candidate(project, accepted)
        return {
            "ok": True,
            "candidate": accepted.model_dump(mode="json"),
            # False when the id already existed in dev.yaml (idempotent re-accept).
            "added_to_dev": bool(added),
        }

    @app.post("/api/candidates/{candidate_id}/reject")
    def reject(candidate_id: str) -> dict[str, object]:
        candidate = _require(project, candidate_id)
        rejected = candidate.model_copy(update={"status": CandidateStatus.rejected})
        update_candidate(project, rejected)
        return {"candidate": rejected.model_dump(mode="json")}

    # --- the judge review surface (spec §7, plan 5.2) --------------------------------------

    @app.get("/api/review")
    def review(split: str = "dev") -> dict[str, object]:
        # Open onto the latest run of the split. Show the rows a human still owns: the
        # deterministic review pile plus any already-resolved (so a call can be revisited).
        run = latest_run(paths, split=split)
        if run is None:
            ag = judge_agreement(project)
            return {
                "run": None,
                "rows": [],
                "agreement": {"n": ag.n, "n_agree": ag.n_agree, "rate": ag.rate},
            }
        rows = [_review_row(r) for r in run.results if r.needs_review or r.reviewed]
        return {"run": _headline(run, project), "rows": rows}

    @app.post("/api/review/{question_id}/resolve")
    def resolve(question_id: str, req: Resolve) -> dict[str, object]:
        if req.verdict not in _HUMAN_VERDICTS:
            raise HTTPException(status_code=422, detail=f"cannot assign verdict {req.verdict!r}")
        run = latest_run(paths, split=req.split)
        if run is None:
            raise HTTPException(status_code=404, detail=f"no {req.split} run to review")
        result = next((r for r in run.results if r.id == question_id), None)
        if result is None:
            raise HTTPException(status_code=404, detail=f"no question {question_id!r} in the run")

        # The human's call is authoritative: it becomes the resolution of record for the run
        # (so resolved_accuracy reflects it), overwriting the same run file in place.
        result.human_verdict = req.verdict
        save_run(paths, run)

        # Calibrate the judge: record whether the human agreed with its suggestion. Only rows
        # the judge actually triaged are calibration data (spec §7).
        if result.judge_suggestion is not None:
            append_calibration(
                project,
                CalibrationRecord(
                    run_id=run.run_id,
                    question_id=question_id,
                    # Split-scoped: a test ruling must never coach the dev judge (invariant 3).
                    split=run.split,
                    judge_suggestion=result.judge_suggestion,
                    human_verdict=req.verdict,
                    agreed=req.verdict is result.judge_suggestion,
                    # Carried so this ruling can coach the judge as a few-shot example.
                    question=result.question,
                    generated_sql=result.generated_sql,
                    gold_sql=result.gold_sql,
                    note=req.note,
                ),
            )
        return {"ok": True, "row": _review_row(result), "run": _headline(run, project)}

    return app


def _require(project: Project, candidate_id: str) -> Candidate:
    candidate = get_candidate(project, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail=f"no candidate {candidate_id!r}")
    return candidate


# Re-exported so tests and the CLI don't reach into the model module for the shape.
__all__ = ["Edit", "ExecutionEvidence", "RerunRequest", "Resolve", "create_app"]
