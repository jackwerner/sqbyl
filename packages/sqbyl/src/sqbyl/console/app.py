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

from sqbyl.candidates_io import (
    get_candidate,
    load_candidates,
    update_candidate,
)
from sqbyl.eval.benchmarks_io import append_to_dev_set
from sqbyl.models import Candidate, CandidateStatus, ExecutionEvidence
from sqbyl.project import Project
from sqbyl.synth import check_gold_sql

_STATIC = Path(__file__).resolve().parent / "static"


class Edit(BaseModel):
    """Optional per-field overrides supplied on accept / re-run (``None`` = keep as-is)."""

    question: str | None = None
    gold_sql: str | None = None
    difficulty: str | None = None
    canonical: bool | None = None


class RerunRequest(BaseModel):
    gold_sql: str


def _apply_edit(candidate: Candidate, edit: Edit) -> Candidate:
    updates = {k: v for k, v in edit.model_dump().items() if v is not None}
    return candidate.model_copy(update=updates) if updates else candidate


def create_app(project: Project) -> FastAPI:
    """Build the review console bound to one loaded ``project``."""
    app = FastAPI(title="sqbyl review", docs_url=None, redoc_url=None)
    dialect = project.manifest.database.dialect

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

    return app


def _require(project: Project, candidate_id: str) -> Candidate:
    candidate = get_candidate(project, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail=f"no candidate {candidate_id!r}")
    return candidate


# Re-exported so tests and the CLI don't reach into the model module for the shape.
__all__ = ["Edit", "ExecutionEvidence", "RerunRequest", "create_app"]
