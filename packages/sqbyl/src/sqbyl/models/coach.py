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

from pydantic import Field

from sqbyl_runtime.llm.base import Usage
from sqbyl_runtime.models import SqbylModel


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
PROSE_LAYERS: frozenset[CoachLayer] = frozenset({CoachLayer.instruction})


class CoachProposal(SqbylModel):
    """One applyable improvement — a file diff with its reasoning (spec §8).

    ``diff`` is a unified diff against ``target_file`` (relative to the project root), so it
    displays as a diff and applies as one (Phase 5.4). ``predicted_fixes`` is how many of the
    addressed ``question_ids`` the Coach expects to flip green, and ``confidence`` how sure it
    is — together the leverage signal that ranks the list."""

    id: str
    title: str
    root_cause: str
    layer: CoachLayer
    target_file: str
    diff: str
    rationale: str = ""
    predicted_fixes: int = Field(default=0, ge=0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    question_ids: list[str] = Field(default_factory=list)
    # The Coach flags conflicts an edit may introduce (e.g. a global instruction that could
    # contradict existing prose), so a reviewer sees the risk before applying (spec §8).
    conflicts: str = ""

    @property
    def is_prose(self) -> bool:
        """True when the edit is a global prose rule — the last-resort layer to prefer against."""
        return self.layer in PROSE_LAYERS

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
