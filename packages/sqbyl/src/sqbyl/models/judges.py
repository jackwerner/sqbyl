"""Layer-2 LLM-judge models (spec §7 Layer 2, plan 5.1).

The judges resolve exactly the rows Layer 1 could not: a result-set *mismatch* or a
question with *no executable gold* — the rows Layer 1 parks at ``manual_review`` rather
than asserting incorrect. Each judge returns a strict-JSON :class:`JudgeVerdict`; the
arbiter (:mod:`sqbyl.judges`) folds those verdicts, together with the Layer-1 signal,
into a final verdict — or leaves the row at ``manual_review`` when it cannot decide with
confidence (spec §7). These are dev-only models (benchmarks and their judgements never
ship in a release), so they live in the ``sqbyl`` package.
"""

from __future__ import annotations

from pydantic import Field

from sqbyl_runtime.models import SqbylModel

# Layer-2 judge names (spec §7). Stable strings so reports/console key off them without
# importing the judge functions — the same convention as the Layer-1 scorer names.
JUDGE_SEMANTIC_EQUIVALENCE = "semantic_equivalence"
JUDGE_LOGICAL_ACCURACY = "logical_accuracy"
JUDGE_COMPLETENESS = "completeness"
JUDGE_ANSWER_QUALITY = "answer_quality"

# The judges that resolve a result-set mismatch when a gold query exists. ``answer_quality``
# is separate: it grades an NL summary and only runs when one was produced, so it is not in
# this default set (spec §7).
GOLD_MISMATCH_JUDGES = (
    JUDGE_SEMANTIC_EQUIVALENCE,
    JUDGE_LOGICAL_ACCURACY,
    JUDGE_COMPLETENESS,
)

# When there is no gold to compare against, semantic-equivalence is meaningless (nothing to
# be equivalent *to*); intent and completeness are still judgeable from the question alone.
NO_GOLD_JUDGES = (
    JUDGE_LOGICAL_ACCURACY,
    JUDGE_COMPLETENESS,
)

ALL_JUDGES = (
    JUDGE_SEMANTIC_EQUIVALENCE,
    JUDGE_LOGICAL_ACCURACY,
    JUDGE_COMPLETENESS,
    JUDGE_ANSWER_QUALITY,
)


class JudgeVerdict(SqbylModel):
    """One judge's verdict on one question — the strict-JSON shape the model returns.

    ``passed`` is the judge's boolean call; ``confidence`` (0–1) is how sure it is, which
    the arbiter uses to decide whether the panel is trustworthy enough to score the row or
    whether it must stay ``manual_review``. ``rationale`` is shown to the human in the
    review console (spec §7) — a verdict without a reason is never actionable.
    """

    judge: str
    passed: bool
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""
