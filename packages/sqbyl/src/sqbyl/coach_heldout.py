"""Guardrailed diagnosis of a **held-out** test failure — `coach --from-test-failure` (§7/§8).

Finding #3: a real failure that only appears in ``benchmarks/test.yaml`` had no sanctioned
path to a reviewed fix. The optimizer/Coach are dev-only by construction (invariant 3), so a
user who scored the held-out set, found a genuine miss, and wanted help had to either edit
semantics by hand or — worse — copy the test question into dev, which is exactly the leakage
the dev/test boundary exists to prevent.

This module closes that dead-end **without** breaking the boundary, per the reviewers' hard
guardrails:

1. **The gold is walled off.** Diagnosis runs from the agent's *own* trace — the question, the
   SQL it generated, its plan, any error, the tables it saw — never the test row's ``gold_sql``
   or gold result. That wall is structural: :class:`HeldoutFailure` has **no gold field**, so
   the diagnoser *cannot* see it even if a caller wanted to leak it. And like the dev Coach,
   this module is in the import-linter ``forbidden`` contract — it may not import
   ``sqbyl.eval.heldout`` (a code boundary, not a convention — invariant 3).
2. **General edits, human-reviewed, never auto-applied.** The proposal is an ordinary Coach
   context edit (examples > semantics > prose); the CLI persists it for review and refuses to
   run under ``--auto``. A fix that only special-cases the one question is leakage wearing a
   diff — the reviewer is told to reject it.
3. **Provenance + quarantine.** Each proposal is stamped ``derived_from_heldout``; the test
   item is recorded in a quarantine so its next held-out score is flagged as no longer an
   independent measurement (the one honest overfitting signal isn't silently corrupted).

The gold never enters the loop, so a fix that generalizes will still show up on the *rest* of
the held-out set — while the inspected item's own number is quarantined.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import Field

from sqbyl.models import CoachProposal, CoachReport
from sqbyl.models.runs import Verdict
from sqbyl_runtime.llm.base import LLMClient, LLMRequest, Message
from sqbyl_runtime.models import Dialect, SqbylModel
from sqbyl_runtime.state.traces import TraceWriter, llm_call_span, new_trace_id

if TYPE_CHECKING:
    from sqbyl.models.runs import QuestionResult
    from sqbyl.project import Project
    from sqbyl_runtime.state.layout import SqbylPaths

_HELDOUT_SYSTEM = (
    "You are the Coach for a text-to-SQL agent, diagnosing ONE failure the agent made on a "
    "HELD-OUT test question. You improve the agent only by editing its project files — never by "
    "editing SQL by hand.\n\n"
    "CRITICAL: you are NOT shown the gold answer for this question, on purpose. You must reason "
    "ONLY from the agent's own trace (its plan, the SQL it generated, any error, the tables it "
    "selected) and the agent's CURRENT project files. Do not ask for the gold; diagnose the "
    "likely root cause from the schema and the agent's reasoning.\n\n"
    "Propose the MINIMAL, highest-leverage edit at the RIGHT layer (most preferred first):\n"
    "  1. examples/     — a worked question→SQL example teaching a REUSABLE pattern\n"
    "  2. trusted/      — a curated, blessed SQL asset\n"
    "  3. semantics/    — a measure, a synonym, a named filter, or a column/table description\n"
    "  4. instructions.md — a GLOBAL PROSE rule (LAST RESORT)\n\n"
    "The edit MUST be a GENERAL improvement that would help this question AND other, unseen "
    "questions that share its root cause — e.g. a column description that disambiguates a "
    "contested term, a synonym, or a measure. DO NOT special-case this one question: an edit "
    "that only works for this exact question (e.g. an example that restates a guessed answer) "
    "is training on the held-out set and MUST NOT be proposed. If you cannot find a general fix "
    "from the trace alone, return NO proposal and say so in the root_cause — a human will "
    "review the benchmark.\n\n"
    "Each proposal targets exactly one named project file (relative path) and expresses the "
    "change as find/replace EDITS: `find` is text copied VERBATIM from the shown file that "
    "uniquely locates the change, `replace` is what it becomes. Use an EMPTY `find` to append "
    "new content. Give predicted_fixes and a confidence in [0,1]."
)


class HeldoutFailure(SqbylModel):
    """A held-out failure reduced to the agent's own trace — **gold-free by construction**.

    This is the only thing :func:`coach_heldout_failure` ever sees, so the test row's gold SQL
    and gold result physically cannot reach the diagnosis or the proposal (finding #3 guardrail
    1). Build it with :meth:`from_question_result`, which deliberately drops the gold fields."""

    id: str
    question: str
    generated_sql: str = ""
    plan: str = ""
    error: str | None = None
    selected_tables: list[str] = Field(default_factory=list)
    verdict: Verdict = Verdict.manual_review

    @classmethod
    def from_question_result(cls, r: QuestionResult) -> HeldoutFailure:
        """Reduce a scored test row to its gold-free trace. Note what is copied — question,
        generated SQL, plan, error, selected tables — and, above all, what is NOT: ``gold_sql``
        and ``gold_asset`` are never read here, which is the wall the whole feature rests on."""
        return cls(
            id=r.id,
            question=r.question,
            generated_sql=r.generated_sql,
            plan=r.plan,
            error=r.error,
            selected_tables=list(r.selected_tables),
            verdict=r.verdict,
        )


def _render_heldout_prompt(project: Project, failure: HeldoutFailure, *, dialect: Dialect) -> str:
    from sqbyl.coach import _render_project_files  # dev-side; never imports heldout

    lines = [
        f"- id: {failure.id}",
        f"  question: {failure.question}",
        f"  verdict: {failure.verdict.value}",
    ]
    if failure.plan:
        lines.append(f"  agent_plan: {failure.plan}")
    lines.append(f"  generated_sql: {failure.generated_sql}")
    if failure.selected_tables:
        lines.append(f"  selected_tables: {', '.join(failure.selected_tables)}")
    if failure.error:
        lines.append(f"  error: {failure.error}")
    lines.append("  (the gold answer is intentionally withheld — diagnose from the trace above)")
    return (
        f"SQL dialect: {dialect.value}\n\n"
        f"HELD-OUT FAILURE (1):\n" + "\n".join(lines) + "\n\n"
        f"CURRENT PROJECT FILES:\n{_render_project_files(project)}\n\n"
        "Propose at most a few ranked, minimal, GENERAL file diffs. Return them all."
    )


def coach_heldout_failure(
    project: Project,
    failure: HeldoutFailure,
    *,
    llm: LLMClient,
    model: str,
    trace_writer: TraceWriter | None = None,
) -> CoachReport:
    """Diagnose one gold-free held-out failure → a ranked :class:`CoachReport` (finding #3).

    Reuses the dev Coach's proposal validation and ranking, but on a single failure whose gold
    is walled off. Every proposal is stamped ``derived_from_heldout=failure.id``. One paid call;
    pure and metering-free (the CLI meters), so it's testable under record-replay."""
    from sqbyl.coach import (
        _CoachDraft,
        _coerce_layer,
        _current_file_text,
        _fingerprint,
        _rank,
        _slug,
        _validate_proposal,
    )
    from sqbyl.models import CoachEdit

    dialect = project.manifest.database.dialect
    trace_id = new_trace_id()
    request = LLMRequest(
        model=model,
        messages=[
            Message(role="user", content=_render_heldout_prompt(project, failure, dialect=dialect))
        ],
        system=_HELDOUT_SYSTEM,
        response_schema=_CoachDraft.model_json_schema(),
        max_tokens=8192,
        temperature=0.0,
        cache_system=True,
    )
    response = llm.complete(request)
    if trace_writer is not None:
        trace_writer.write(
            llm_call_span(
                request, response, operation="chat", name="coach_heldout", trace_id=trace_id
            )
        )
    drafts = response.parse(_CoachDraft).proposals
    taken: set[str] = set()
    proposals = [
        CoachProposal(
            id=_slug(d.title, taken=taken),
            title=d.title,
            root_cause=d.root_cause,
            layer=_coerce_layer(d.layer),
            target_file=d.target_file,
            edits=[CoachEdit(find=e.find, replace=e.replace) for e in d.edits],
            target_fingerprint=_fingerprint(_current_file_text(project, d.target_file)),
            rationale=d.rationale,
            predicted_fixes=max(0, d.predicted_fixes),
            confidence=min(1.0, max(0.0, d.confidence)),
            question_ids=[failure.id],
            conflicts=d.conflicts,
            derived_from_heldout=failure.id,
        )
        for d in drafts
    ]
    proposals = [_validate_proposal(project, p) for p in proposals]
    return CoachReport(
        run_id=f"heldout:{failure.id}",
        model=model,
        n_failures=1,
        proposals=_rank(proposals),
        usage=response.usage,
    )


# --- quarantine: a held-out item inspected to derive a fix is no longer an independent measure -

_QUARANTINE_FILE = "heldout_quarantine.json"


class QuarantineRecord(SqbylModel):
    """One held-out test id whose score is compromised because a human inspected it to coach."""

    question_id: str
    reason: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def record_quarantine(paths: SqbylPaths, question_id: str, *, reason: str = "") -> None:
    """Mark a held-out test id as quarantined (idempotent — re-marking refreshes the reason).

    Persisted to ``.sqbyl/heldout_quarantine.json`` so `eval test` can warn that the item's
    score is no longer an independent measurement (finding #3 guardrail 3)."""
    current = {r.question_id: r for r in load_quarantine(paths)}
    current[question_id] = QuarantineRecord(question_id=question_id, reason=reason)
    path = paths.root / _QUARANTINE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([r.model_dump(mode="json") for r in current.values()], indent=2) + "\n"
    )


def load_quarantine(paths: SqbylPaths) -> list[QuarantineRecord]:
    """Every quarantined held-out id, or an empty list when none are recorded."""
    path = paths.root / _QUARANTINE_FILE
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [QuarantineRecord.model_validate(item) for item in raw]


def quarantined_ids(paths: SqbylPaths) -> set[str]:
    return {r.question_id for r in load_quarantine(paths)}
