"""The Coach — LLM-assisted iteration over the agent's project files (spec §8, plan 5.3).

The headline feature. Given a scored **dev** run, the Coach reads each failing or
still-unresolved question — the agent's plan and SQL, the gold, the scorer verdicts, any
execution error — alongside the *current* project files and the inherited best-practice
rubric, then proposes a **ranked list of applyable file diffs**: the minimal, highest-
leverage edit at the *right layer* of the metadata hierarchy (examples > semantics >
prose). Each proposal is a diff, not advice; ``sqbyl coach apply`` (Phase 5.4) writes them.

**Dev-only, by construction (invariant 3).** The Coach optimizes on dev; showing it the
held-out ``test.yaml`` would be training on the test set. It only ever receives a dev
:class:`~sqbyl.models.ScoredRun` (asserted here), and this module must never import
:mod:`sqbyl.eval.heldout` — enforced by the import-linter ``forbidden`` contract that now
lists ``sqbyl.coach`` alongside synth/console.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from pydantic import BaseModel, Field

from sqbyl.models import (
    LAYER_PREFERENCE,
    CoachEdit,
    CoachLayer,
    CoachProposal,
    CoachReport,
    QuestionResult,
    ScoredRun,
    Verdict,
)
from sqbyl.project import Project
from sqbyl_runtime.llm.base import LLMClient, LLMRequest, Message
from sqbyl_runtime.models import Dialect
from sqbyl_runtime.state.layout import SqbylPaths
from sqbyl_runtime.state.traces import TraceWriter, llm_call_span, new_trace_id

_SYSTEM = (
    "You are the Coach for a text-to-SQL agent. You improve the agent by editing its project "
    "files — never by editing SQL by hand. You are shown the questions the agent got wrong on "
    "its DEV benchmark (its plan, its generated SQL, the gold answer, the scorer verdicts, and "
    "any error) and the agent's CURRENT project files.\n\n"
    "Your job: cluster the failures by ROOT CAUSE, then for each cluster propose the MINIMAL, "
    "highest-leverage edit at the RIGHT layer of this hierarchy (most preferred first):\n"
    "  1. examples/     — a worked question→SQL example (sets the accuracy ceiling)\n"
    "  2. trusted/      — a curated, blessed SQL asset\n"
    "  3. semantics/    — a measure, a synonym, a named filter, or a column/table description\n"
    "  4. instructions.md — a GLOBAL PROSE rule (LAST RESORT: prose conflicts and generalizes "
    "poorly; never reach for it when a measure, synonym, description, or example would fix it)\n\n"
    "Rules: (1) prefer data/examples over prose; (2) keep every edit minimal and focused; "
    "(3) flag any conflict an edit could introduce with existing metadata; (4) rank proposals "
    "by leverage — how many failing questions each fixes and how confident you are; (5) each "
    "proposal targets exactly one named project file (relative path) and expresses the change "
    "as one or more find/replace EDITS: `find` is text copied VERBATIM from the shown file that "
    "uniquely locates the change, `replace` is what it becomes. Use an EMPTY `find` to append "
    "new content to the end of the file (or to create a new file). Copy anchors exactly — "
    "whitespace included. Give predicted_fixes and a confidence in [0,1].\n\n"
    "GENERALIZE, don't memorize. When several failures share a root cause, prefer ONE general "
    "fix (a measure, synonym, description, or named filter) that covers the whole cluster. Add "
    "an examples/ entry only when it teaches a reusable PATTERN that will also help questions "
    "you have NOT been shown — never an example that merely restates the gold SQL of a single "
    "failing question. Copying one question's gold into an example inflates the dev score "
    "without generalizing to the held-out set; that is training on the benchmark, not fixing "
    "the agent.\n\n"
    "A mismatch is NOT proof the agent is wrong. Some failing rows are UNRESOLVED — the "
    "generated SQL may be semantically equivalent to the gold, or the gold itself may be "
    "wrong. Each row is labelled with its status. For an unresolved row where the agent's SQL "
    "looks correct, do NOT contort the agent's context to match a questionable gold — return "
    "no proposal for it and let a human review the benchmark. Only propose context edits for "
    "failures that are genuinely the agent's fault."
)


class _EditDraft(BaseModel):
    find: str = ""
    replace: str


class _ProposalDraft(BaseModel):
    """One proposal as the model returns it (before it gets a stable id)."""

    title: str
    root_cause: str
    layer: str
    target_file: str
    edits: list[_EditDraft] = Field(default_factory=list)
    rationale: str = ""
    predicted_fixes: int = 0
    confidence: float = 0.0
    question_ids: list[str] = Field(default_factory=list)
    conflicts: str = ""


class _CoachDraft(BaseModel):
    """The structured result of the single paid Coach call."""

    proposals: list[_ProposalDraft] = Field(default_factory=list)


def gather_failures(run: ScoredRun) -> list[QuestionResult]:
    """The dev rows worth coaching: everything not *resolved* correct.

    A deterministic pass, or a ``manual_review`` a human confirmed correct, is not a failure
    and is skipped; an error, an unresolved review-pile row, or a human-refuted row is (spec
    §8). Uses ``resolved_correct`` so human review is honored."""
    return [r for r in run.results if not r.resolved_correct]


def _render_file(path: str, text: str) -> str:
    return f"===== {path} =====\n{text.rstrip()}\n"


def _render_project_files(project: Project) -> str:
    """The agent's current project files, verbatim with relative paths — the Coach diffs
    against these, so it needs their exact text.

    ALLOWLIST, on purpose: only the agent's *context* files (semantics / examples / trusted /
    instructions). It must never include ``benchmarks/`` — feeding the Coach ``test.yaml``
    would be training on the held-out set (invariant 3). Keep this an allowlist; never widen
    it to "all project files"."""
    blocks: list[str] = []
    for sub in ("semantics", "examples", "trusted"):
        d = project.root / sub
        if d.is_dir():
            for f in sorted(d.glob("*.yaml")) + sorted(d.glob("*.sql")):
                blocks.append(_render_file(f"{sub}/{f.name}", f.read_text()))
    instructions = project.root / "instructions.md"
    if instructions.exists():
        blocks.append(_render_file("instructions.md", instructions.read_text()))
    return "\n".join(blocks)


def _failure_status(r: QuestionResult) -> str:
    """A one-line status telling the Coach how much to trust that this row is really a bug —
    so it doesn't "fix" a false failure (an equivalent-SQL mismatch or a wrong gold)."""
    if r.verdict is Verdict.error:
        return "CONFIRMED — the agent produced no runnable SQL; this is the agent's fault"
    if r.human_verdict is Verdict.incorrect:
        return "CONFIRMED WRONG — a human ruled this incorrect"
    if r.judge_suggestion is Verdict.correct:
        return (
            "UNRESOLVED — the judge suspects the agent's SQL is actually EQUIVALENT to gold "
            "(the benchmark gold may be wrong); do not force a context edit — flag for a human"
        )
    if r.judge_suggestion is Verdict.incorrect:
        return "LIKELY WRONG — the judge suspects the agent's SQL is incorrect"
    return (
        "UNRESOLVED mismatch — a different result set is not proof of error; the agent's SQL "
        "may be equivalent. Only edit context if it is genuinely the agent's fault"
    )


def _render_failure(r: QuestionResult) -> str:
    lines = [
        f"- id: {r.id}",
        f"  question: {r.question}",
        f"  verdict: {r.verdict.value}",
        f"  status: {_failure_status(r)}",
    ]
    if r.plan:
        lines.append(f"  agent_plan: {r.plan}")
    lines.append(f"  generated_sql: {r.generated_sql}")
    if r.gold_sql:
        lines.append(f"  gold_sql: {r.gold_sql}")
    if r.gold_asset:
        lines.append(f"  gold_asset: {r.gold_asset}")
    if r.selected_tables:
        lines.append(f"  selected_tables: {', '.join(r.selected_tables)}")
    for s in r.scorers:
        if s.passed is False:
            lines.append(f"  scorer {s.name}: FAIL — {s.detail or ''}")
    if r.error:
        lines.append(f"  error: {r.error}")
    return "\n".join(lines)


def _render_prompt(project: Project, failures: list[QuestionResult], *, dialect: Dialect) -> str:
    fail_block = "\n".join(_render_failure(r) for r in failures)
    return (
        f"SQL dialect: {dialect.value}\n\n"
        f"FAILING DEV QUESTIONS ({len(failures)}):\n{fail_block}\n\n"
        f"CURRENT PROJECT FILES:\n{_render_project_files(project)}\n\n"
        "Propose the ranked, minimal file diffs that fix the most failures. Return them all."
    )


def _coerce_layer(value: str) -> CoachLayer:
    """Map the model's layer string to a :class:`CoachLayer`; an unrecognized value is
    treated as the last-resort prose layer so it can't masquerade as high-leverage."""
    try:
        return CoachLayer(value.strip().lower())
    except ValueError:
        return CoachLayer.instruction


def _slug(title: str, *, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:48] or "proposal"
    candidate = base
    i = 2
    while candidate in taken:
        candidate = f"{base}_{i}"
        i += 1
    taken.add(candidate)
    return candidate


def coach(
    project: Project,
    run: ScoredRun,
    *,
    llm: LLMClient,
    model: str,
    trace_writer: TraceWriter | None = None,
) -> CoachReport:
    """Read a scored **dev** run's failures → a ranked :class:`CoachReport` (spec §8).

    One paid structured call. Pure and metering-free (the CLI meters), so it's testable
    under record-replay with an injected client (invariant 4). Refuses a non-dev run: the
    Coach must never be shown the held-out set (invariant 3)."""
    if run.split != "dev":
        raise ValueError(
            f"the Coach only runs on the dev set; got a {run.split!r} run (invariant 3)"
        )
    failures = gather_failures(run)
    if not failures:
        # A clean dev run has nothing to coach — never spend a token to say "no proposals".
        return CoachReport(run_id=run.run_id, model=model, n_failures=0)
    dialect = project.manifest.database.dialect
    trace_id = new_trace_id()
    request = LLMRequest(
        model=model,
        messages=[Message(role="user", content=_render_prompt(project, failures, dialect=dialect))],
        system=_SYSTEM,
        response_schema=_CoachDraft.model_json_schema(),
        max_tokens=8192,
        temperature=0.0,
        cache_system=True,
    )
    response = llm.complete(request)
    if trace_writer is not None:
        trace_writer.write(
            llm_call_span(request, response, operation="chat", name="coach", trace_id=trace_id)
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
            question_ids=d.question_ids,
            conflicts=d.conflicts,
        )
        for d in drafts
    ]
    return CoachReport(
        run_id=run.run_id,
        model=model,
        source_models=dict(run.models),
        source_calibration=run.judge_calibration,
        n_failures=len(failures),
        proposals=_rank(proposals),
        usage=response.usage,
    )


def _rank(proposals: list[CoachProposal]) -> list[CoachProposal]:
    """Deterministic leverage ranking — not the order the model happened to emit (spec §8).

    Sinks the two low-trust layers (a last-resort prose rule, and a single-question example
    that likely just memorizes gold), then orders by the Coach's claimed leverage
    (``predicted_fixes``, an *unvalidated* self-report — see the CLI labelling), then the
    metadata-hierarchy preference. Stable and reproducible from the proposals alone."""
    return sorted(
        proposals,
        key=lambda p: (
            p.is_prose,
            p.memorization_risk,
            -p.predicted_fixes,
            LAYER_PREFERENCE.index(p.layer),
            p.id,
        ),
    )


# --- persistence: the report is saved so `sqbyl coach apply N` (Phase 5.4) can act on it ---


def coach_dir(paths: SqbylPaths) -> Path:
    return paths.root / "coach"


def save_report(paths: SqbylPaths, report: CoachReport) -> Path:
    """Persist a Coach report to ``.sqbyl/coach/``; filename sorts chronologically."""
    paths.ensure()
    d = coach_dir(paths)
    d.mkdir(exist_ok=True)
    path = d / f"{report.created_at:%Y%m%dT%H%M%S}-{report.run_id[:8]}.json"
    path.write_text(report.model_dump_json(indent=2) + "\n")
    return path


def load_reports(paths: SqbylPaths) -> list[CoachReport]:
    """Every persisted Coach report, oldest first."""
    d = coach_dir(paths)
    if not d.is_dir():
        return []
    reports = [CoachReport.model_validate_json(p.read_text()) for p in d.glob("*.json")]
    return sorted(reports, key=lambda r: (r.created_at, r.run_id))


def latest_report(paths: SqbylPaths) -> CoachReport | None:
    reports = load_reports(paths)
    return reports[-1] if reports else None


# --- apply: write a chosen proposal's edits to the project files (`coach apply`, plan 5.4) ---


class ApplyError(Exception):
    """A proposal's edit could not be applied safely (bad target, ambiguous/missing anchor)."""


def _fingerprint(text: str) -> str:
    """A short content hash; ``""`` for an absent/empty file (so 'no file' fingerprints stably)."""
    return hashlib.sha256(text.encode()).hexdigest()[:12] if text else ""


def _current_file_text(project: Project, target_file: str) -> str:
    """The current text of a target file, or ``""`` if it's absent or an unsafe target — used
    to fingerprint at coach time so `apply` can detect drift."""
    try:
        path = _resolve_target(project, target_file)
    except ApplyError:
        return ""
    return path.read_text() if path.exists() else ""


# The only files `coach apply` may write: the agent's *context*. An ALLOWLIST, not a
# benchmarks denylist — so the Coach can never touch the held-out ``test.yaml`` (invariant 3),
# the manifest (``sqbyl.yaml``, which holds DB config), or ``.sqbyl/`` state, even via a
# lookalike or traversal path.
#
# Safety comes from the exact-match allowlist FAILING CLOSED: anything whose first resolved
# path component isn't one of these is refused — ``Benchmarks/`` (on a case-insensitive FS),
# ``benchmarksX/``, ``sqbyl.yaml``, a ``..`` escape, a symlink out. Note ``.resolve()`` does
# NOT case-fold, so a variant-case *writable* target (``SEMANTICS/x.yaml``) is also refused —
# intentionally. Do NOT "fix" that by lowercasing the allowlist: that would reopen the
# ``Benchmarks/`` → ``benchmarks/`` bypass on case-insensitive filesystems.
_WRITABLE_SUBDIRS = ("semantics", "examples", "trusted")
_WRITABLE_FILES = ("instructions.md",)


def _resolve_target(project: Project, target_file: str) -> Path:
    """Resolve a proposal's relative ``target_file`` to a path the Coach is allowed to write,
    or raise :class:`ApplyError`. Refuses anything outside the project or outside the writable
    context surface (see :data:`_WRITABLE_SUBDIRS` / :data:`_WRITABLE_FILES`)."""
    root = project.root.resolve()
    path = (root / target_file).resolve()  # absolute/`..`/symlink all normalize here
    try:
        rel = path.relative_to(root)
    except ValueError:
        raise ApplyError(f"refusing to edit {target_file!r}: outside the project") from None
    writable = (len(rel.parts) >= 2 and rel.parts[0] in _WRITABLE_SUBDIRS) or (
        str(rel) in _WRITABLE_FILES
    )
    if not writable:
        raise ApplyError(
            f"refusing to edit {target_file!r}: the Coach only edits the agent's context "
            f"({', '.join(_WRITABLE_SUBDIRS)}/, {', '.join(_WRITABLE_FILES)}) — never "
            "benchmarks, the manifest, or .sqbyl state"
        )
    return path


def _apply_edits(text: str, proposal: CoachProposal) -> str:
    """Apply every edit to ``text`` in order, or raise — never a partial/fuzzy application.

    An empty ``find`` appends ``replace`` (with a blank-line separator); otherwise ``find``
    must occur **exactly once** (a miss or an ambiguous match is refused, not guessed)."""
    for i, edit in enumerate(proposal.edits, start=1):
        if not edit.find:
            body = edit.replace if edit.replace.endswith("\n") else edit.replace + "\n"
            if text == "":
                text = body  # new (or empty) file
            else:
                prefix = text if text.endswith("\n") else text + "\n"
                text = prefix + "\n" + body  # blank-line separator before the appended block
            continue
        count = text.count(edit.find)
        if count == 0:
            raise ApplyError(
                f"edit {i} of {proposal.id!r}: anchor not found in {proposal.target_file}"
            )
        if count > 1:
            raise ApplyError(
                f"edit {i} of {proposal.id!r}: anchor matches {count}× in {proposal.target_file} "
                "(ambiguous) — not applied"
            )
        text = text.replace(edit.find, edit.replace, 1)
    return text


def apply_proposal(project: Project, proposal: CoachProposal, *, force: bool = False) -> Path:
    """Write one proposal's edits to its target file → the changed path (plan 5.4).

    Validates and computes the whole new file before writing, so a failing edit leaves the
    file untouched. The result is an ordinary working-tree change: review it with ``git diff``,
    undo it with ``git checkout``/``git revert`` — the Coach never commits.

    Refuses (unless ``force``) when the target file has **drifted** from what the Coach saw —
    a stale append could otherwise land on a since-changed file. A miss on a non-empty
    ``find`` already fails loudly; the fingerprint catches the empty-``find`` append case.

    Not idempotent by itself: re-applying is guarded one level up, by the persisted
    ``applied_at`` marker in ``sqbyl coach apply`` (a second direct call here would be caught
    by the drift check — the first write changes the file — but the CLI is the real guard)."""
    path = _resolve_target(project, proposal.target_file)
    before = path.read_text() if path.exists() else ""
    if (
        not force
        and proposal.target_fingerprint
        and _fingerprint(before) != proposal.target_fingerprint
    ):
        raise ApplyError(
            f"{proposal.target_file} changed since the Coach saw it — re-run `sqbyl coach` "
            "to refresh the proposal (or pass --force to apply anyway)"
        )
    after = _apply_edits(before, proposal)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(after)
    return path
