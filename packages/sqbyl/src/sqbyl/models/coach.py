"""Coach models — ranked, applyable improvement proposals (spec §8, plan 5.3).

The Coach reads an eval run's **dev** failures and proposes the *minimal, highest-leverage*
edit at the *right layer of the metadata hierarchy* (examples > semantics > prose). Each
proposal is a concrete file diff, not advice — so a human (or ``sqbyl coach apply``, Phase
5.4) can apply it and re-eval. These are dev-only models: the Coach never sees ``test.yaml``
(invariant 3), so they live in the ``sqbyl`` dev package.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import Field

from sqbyl_runtime.llm.base import Usage
from sqbyl_runtime.models import SqbylModel

# The one global-prose file (spec §8). "Is this edit prose?" is a fact about *where it writes*,
# not about the layer the model self-reported — so `is_prose` derives from `target_file`.
PROSE_FILE = "instructions.md"


class CoachLayer(StrEnum):
    """Which layer of the metadata hierarchy an edit targets (spec §1.5, §8).

    Ordered by the "examples > semantics > prose" preference: the Coach should reach for a
    higher-leverage, lower-risk layer before a global instruction. ``instruction`` is prose —
    the last resort, because free-text rules conflict and generalize poorly."""

    example = "example"  # a few-shot example — the accuracy ceiling (most preferred)
    trusted_asset = "trusted_asset"  # a curated, blessed query
    measure = "measure"  # a reusable metric definition
    synonym = "synonym"  # a column/table synonym mapping user words to schema
    named_filter = "named_filter"  # a reusable WHERE definition
    column_description = "column_description"  # clarify one column's meaning
    table_description = "table_description"  # clarify one table's meaning
    instruction = "instruction"  # global prose rule — last resort


# Leverage/preference order for sorting and for warning when the Coach reaches for prose.
LAYER_PREFERENCE: tuple[CoachLayer, ...] = (
    CoachLayer.example,
    CoachLayer.trusted_asset,
    CoachLayer.measure,
    CoachLayer.synonym,
    CoachLayer.named_filter,
    CoachLayer.column_description,
    CoachLayer.table_description,
    CoachLayer.instruction,
)


class CoachEdit(SqbylModel):
    """One find/replace edit to one file — the reliably-applyable unit (spec §8, plan 5.4).

    ``find`` is text copied **verbatim** from the current file that uniquely locates the
    change; ``replace`` is what it becomes. An empty ``find`` **appends** ``replace`` to the
    file (creating it if it doesn't exist yet) — how a new measure, example, or asset is
    added. Search/replace (not a line-numbered patch) so application is exact and testable:
    the anchor either matches once or the edit is refused, never applied fuzzily."""

    find: str = ""
    replace: str


class CoachProposal(SqbylModel):
    """One applyable improvement — a set of file edits with its reasoning (spec §8).

    The edits target one ``target_file`` (relative to the project root). They are structured
    find/replace pairs, not a free-text patch, so ``sqbyl coach apply`` applies them exactly;
    :meth:`render_diff` derives a human-readable diff *from the edits*, so what a reviewer
    sees is exactly what will be written. ``predicted_fixes`` is how many of the addressed
    ``question_ids`` the Coach expects to flip green, and ``confidence`` how sure it is."""

    id: str
    title: str
    root_cause: str
    layer: CoachLayer
    target_file: str
    edits: list[CoachEdit] = Field(default_factory=list)
    rationale: str = ""
    predicted_fixes: int = Field(default=0, ge=0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    question_ids: list[str] = Field(default_factory=list)
    # The Coach flags conflicts an edit may introduce (e.g. a global instruction that could
    # contradict existing prose), so a reviewer sees the risk before applying (spec §8).
    conflicts: str = ""
    # A fingerprint of ``target_file``'s content when the Coach saw it, so `coach apply` can
    # refuse a stale edit whose file has since drifted (empty ⇒ the file didn't exist yet).
    target_fingerprint: str = ""
    # Stamped by `coach apply` once written, so a re-apply is caught (an empty-`find` append
    # would otherwise silently duplicate the edit) and the audit trail records what was applied.
    applied_at: datetime | None = None
    # Provenance for a proposal diagnosed from a *held-out* test failure (finding #3): the test
    # question's id. Set only on the guardrailed `coach --from-test-failure` path — it means the
    # edit was informed by inspecting a held-out row (never its gold), so a reviewer knows the
    # item's next test score is no longer an independent measurement. ``None`` on the dev loop.
    derived_from_heldout: str | None = None

    def render_diff(self) -> str:
        """A human-readable diff derived from the edits (display only — apply uses the edits).

        Deriving it from the edits guarantees the shown diff equals what gets written."""
        lines: list[str] = []
        for e in self.edits:
            for line in e.find.splitlines():
                lines.append(f"- {line}")
            for line in e.replace.splitlines():
                lines.append(f"+ {line}")
        return "\n".join(lines)

    @property
    def is_prose(self) -> bool:
        """True when the edit actually writes the global-prose file (``instructions.md``).

        Derived from ``target_file`` — the thing that gets written — not the model's self-reported
        ``layer``. The model sometimes mislabels a well-targeted structured edit (a real
        ``semantics/*.yaml`` column change) as ``layer=instruction``; trusting that stamped the
        edit with the "⚠ global prose — last resort" flag a reviewer is trained to skip, and
        force-routed it to human review (finding UX). Whatever the model called the layer, an edit
        that doesn't touch ``instructions.md`` is not prose."""
        return PurePosixPath(self.target_file).name == PROSE_FILE

    @property
    def memorization_risk(self) -> bool:
        """A single-question example that likely just encodes that question's gold SQL.

        Such an edit flips one dev row green while teaching nothing that generalizes — it
        inflates the dev score without moving the held-out set (the classic "training on the
        benchmark"). Flagged so it's ranked last and shown with a warning, the same posture
        as :attr:`is_prose`."""
        return self.layer is CoachLayer.example and len(self.question_ids) <= 1


class CoachReport(SqbylModel):
    """The Coach's ranked output for one eval run (spec §8).

    Proposals arrive already ranked by the Coach (highest-leverage first). Persisted to
    ``.sqbyl/coach/`` so ``sqbyl coach apply N`` (Phase 5.4) can apply a chosen subset."""

    run_id: str  # the eval run whose failures this was computed from
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    model: str = ""  # the coach model that produced these proposals
    # The models/calibration of the run being coached — a proposal only makes sense relative
    # to the agent version that produced the failures, so it's stamped here (spec §7/§11), the
    # same "a score is never divorced from what produced it" discipline as ScoredRun.
    source_models: dict[str, str] = Field(default_factory=dict)
    source_calibration: str | None = None
    n_failures: int = 0
    proposals: list[CoachProposal] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)

    @property
    def n_proposals(self) -> int:
        return len(self.proposals)

    @property
    def total_predicted_fixes(self) -> int:
        return sum(p.predicted_fixes for p in self.proposals)

    def proposal(self, proposal_id: str) -> CoachProposal | None:
        return next((p for p in self.proposals if p.id == proposal_id), None)
