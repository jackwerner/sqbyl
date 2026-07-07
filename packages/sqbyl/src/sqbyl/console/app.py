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

from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from sqbyl.attention import (
    decisions_from_coach_report,
    decisions_from_review_pile,
    route,
)
from sqbyl.calibration_io import append_calibration, judge_agreement
from sqbyl.candidates_io import (
    get_candidate,
    load_candidates,
    update_candidate,
)
from sqbyl.coach import ApplyError, apply_proposal, latest_report, save_report
from sqbyl.eval.benchmarks_io import append_to_dev_set
from sqbyl.eval.report import latest_run, save_run
from sqbyl.models import (
    CalibrationRecord,
    Candidate,
    CandidateStatus,
    ExecutionEvidence,
    QuestionResult,
    ReadinessSignal,
    ScoredRun,
    Verdict,
)
from sqbyl.project import Project
from sqbyl.synth import check_gold_sql
from sqbyl_runtime.models import Dialect
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


def _reground_sql(
    project: Project, sql: str, *, dialect: Dialect
) -> tuple[ExecutionEvidence | None, dict[str, object] | None]:
    """Execute ``sql`` read-only to (re)ground it, returning ``(evidence, error)`` with exactly
    one non-``None`` (spec §6.A).

    Opening the DB per request can fail for reasons the reviewer can fix from the shell —
    ``DATABASE_URL`` unset, the DB restarted, a rotated credential, a network blip — so a
    connection failure is translated into a typed ``{"ok": False, "reason": "db_error", ...}``
    result (finding #9), the same shape the console already returns for a bad edit, rather than
    propagating as an unhandled 500 the browser can't explain.

    The client gets an actionable-but-generic message (it points at the fix without piping the
    raw exception — which can carry the connection string / host — into the HTTP response); the
    full error is printed to the console's own stdout for the local operator to read."""
    try:
        with project.connect() as db:
            evidence, reason, detail = check_gold_sql(db, sql, dialect=dialect)
    except Exception as exc:  # connection: env unset / DB down / credential rotated / network
        # Log the real cause where the operator launched `sqbyl review`; keep it out of the
        # response body so a proxied/shared console can't leak connection internals.
        print(f"[sqbyl review] database connection failed: {exc!r}")
        return None, {
            "ok": False,
            "reason": "db_error",
            "detail": (
                "could not reach the database — check the connection (e.g. DATABASE_URL) and "
                "that it's running, then reload. See the `sqbyl review` terminal for details."
            ),
        }
    if evidence is None:
        return None, {"ok": False, "reason": reason.value if reason else "error", "detail": detail}
    return evidence, None


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


def _readiness_view(sig: ReadinessSignal) -> dict[str, object]:
    """The readiness meter for the client — fields plus the derived/labelled bits.

    ``headline`` and the ``reached``/``low_confidence`` flags are computed properties, so they
    have to be added explicitly; the ``~…(projected)`` framing keeps the estimate honest."""
    return {
        **sig.model_dump(mode="json"),
        "reached": sig.reached,
        "low_confidence": sig.low_confidence,
        "headline": sig.headline(),
    }


def _queue_payload(project: Project, paths: SqbylPaths) -> dict[str, object]:
    """Assemble the leverage-sorted attention queue from real artifacts (spec §5.5, plan 6.3).

    The console opens onto this: the latest **dev** run's review pile + the latest Coach
    report's not-yet-applied proposals, routed into auto-applied vs. a leverage-sorted queue
    with a live readiness meter on top. Dev-only — the held-out test run is never assembled
    here (invariant 3)."""
    run = latest_run(paths, split="dev")
    report = latest_report(paths)
    total = run.total if run is not None else 0

    decisions = []
    if report is not None:
        pending = report.model_copy(
            update={"proposals": [p for p in report.proposals if p.applied_at is None]}
        )
        decisions += decisions_from_coach_report(pending, total=total)
    if run is not None:
        decisions += decisions_from_review_pile(run)

    defaults = project.manifest.defaults
    q = route(
        decisions,
        n_correct=run.n_correct if run is not None else 0,
        n=total,
        target=defaults.readiness_target,
        auto_apply_threshold=defaults.auto_apply_threshold,
    )
    return {
        "readiness": _readiness_view(q.readiness),
        "auto_applied": [d.model_dump(mode="json") for d in q.auto_applied],
        "queue": [d.model_dump(mode="json") for d in q.queue],
    }


def _record_resolution(
    project: Project,
    paths: SqbylPaths,
    run: ScoredRun,
    result: QuestionResult,
    verdict: Verdict,
    note: str,
) -> None:
    """Persist a human's authoritative call on a judged row + feed the judge calibration set.

    Shared by the judge-review surface and the queue's accept path so both resolve a row the
    same way: the human verdict becomes the resolution of record (flipping ``resolved_accuracy``,
    never the deterministic floor), and — if the judge triaged the row — the agreement is logged
    split-scoped (a test ruling must never coach the dev judge, invariant 3)."""
    result.human_verdict = verdict
    save_run(paths, run)
    if result.judge_suggestion is not None:
        append_calibration(
            project,
            CalibrationRecord(
                run_id=run.run_id,
                question_id=result.id,
                split=run.split,
                judge_suggestion=result.judge_suggestion,
                human_verdict=verdict,
                agreed=verdict is result.judge_suggestion,
                question=result.question,
                generated_sql=result.generated_sql,
                gold_sql=result.gold_sql,
                note=note,
            ),
        )


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
        evidence, err = _reground_sql(project, req.gold_sql, dialect=dialect)
        if err is not None:
            return err
        assert evidence is not None
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
        evidence, err = _reground_sql(project, edited.gold_sql, dialect=dialect)
        if err is not None:
            return err
        assert evidence is not None
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

        # The human's call is authoritative: it becomes the resolution of record (so
        # resolved_accuracy reflects it) and feeds the judge calibration set.
        _record_resolution(project, paths, run, result, req.verdict, req.note)
        return {"ok": True, "row": _review_row(result), "run": _headline(run, project)}

    # --- the attention queue (spec §5.5, plan 6.3) -----------------------------------------

    @app.get("/api/queue")
    def queue() -> dict[str, object]:
        # The console's front door: the leverage-sorted queue + live readiness meter.
        return _queue_payload(project, paths)

    @app.post("/api/queue/{decision_id}/accept")
    def accept_decision(decision_id: str) -> dict[str, object]:
        # Accepting a card acts on the artifact it came from, then returns the fresh queue so
        # the meter moves live: a Coach card applies its edit; a judge card confirms the
        # advisory suggestion as the human's call. The card then drops out of the next assemble.
        if decision_id.startswith("coach:"):
            report = latest_report(paths)
            proposal = report.proposal(decision_id[len("coach:") :]) if report else None
            if report is None or proposal is None:
                raise HTTPException(
                    status_code=404, detail=f"no coach proposal for {decision_id!r}"
                )
            # Mirror the CLI guard: refuse a re-apply (an empty-`find` append would otherwise
            # silently duplicate). The queue already filters applied proposals out, so this only
            # bites a stale re-POST.
            if proposal.applied_at is not None:
                return {
                    "ok": False,
                    "detail": "already applied",
                    **_queue_payload(project, paths),
                }
            try:
                path = apply_proposal(project, proposal)
            except ApplyError as exc:
                return {"ok": False, "detail": str(exc), **_queue_payload(project, paths)}
            proposal.applied_at = datetime.now(UTC)  # stamp + persist the audit trail
            save_report(paths, report)
            # Name the changed file so the client can show the (git-based) undo path — an
            # applied edit is an ordinary working-tree change, never a hidden mutation.
            rel = path.relative_to(project.root.resolve())
            return {"ok": True, "changed": str(rel), **_queue_payload(project, paths)}

        if decision_id.startswith("judge:"):
            run = latest_run(paths, split="dev")
            prefix = f"judge:{run.run_id}:" if run is not None else None
            # Parse the question id off the *left* (past the known run_id), so a question id
            # that itself contains a colon can't be truncated into the wrong row.
            qid = decision_id[len(prefix) :] if prefix and decision_id.startswith(prefix) else None
            result = next((r for r in run.results if r.id == qid), None) if run and qid else None
            if run is None or result is None:
                raise HTTPException(status_code=404, detail=f"no review row for {decision_id!r}")
            # Accept = confirm the judge's advisory suggestion; a genuinely-ambiguous row with
            # no suggestion stays in review (the human still owns it).
            verdict = result.judge_suggestion or Verdict.manual_review
            _record_resolution(project, paths, run, result, verdict, "")
            return {"ok": True, **_queue_payload(project, paths)}

        raise HTTPException(status_code=422, detail=f"cannot accept decision {decision_id!r}")

    return app


def _require(project: Project, candidate_id: str) -> Candidate:
    candidate = get_candidate(project, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail=f"no candidate {candidate_id!r}")
    return candidate


# Re-exported so tests and the CLI don't reach into the model module for the shape.
__all__ = ["Edit", "ExecutionEvidence", "RerunRequest", "Resolve", "create_app"]
