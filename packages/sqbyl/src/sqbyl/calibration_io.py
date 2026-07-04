"""The judge calibration set — ``.sqbyl/calibration.jsonl`` (spec §7, plan 5.2).

Every time a human confirms or overrides a judged row in the review console, we append a
:class:`CalibrationRecord` here: what the judge suggested, what the human decided, and
whether they agreed. Accumulated, these give the live **judge↔human agreement** score —
the number that tells a curator how far to trust the judge on rows nobody has reviewed
(standard inter-rater agreement between the LLM judge and human reviewers).

Append-only JSONL: the calibration set is an audit trail, not mutable state, so a review
is never silently rewritten. This is dev-loop data (it only exists because a human is
reviewing benchmarks), so the path is derived here rather than in the runtime layout.
"""

from __future__ import annotations

from pathlib import Path

from sqbyl.models import CalibrationRecord, JudgeAgreement
from sqbyl.project import Project
from sqbyl_runtime.state.layout import SqbylPaths


def calibration_path(project: Project) -> Path:
    return SqbylPaths(project.root).root / "calibration.jsonl"


def load_calibration(project: Project) -> list[CalibrationRecord]:
    """Every recorded review, oldest first. Empty when nothing has been reviewed yet."""
    path = calibration_path(project)
    if not path.exists():
        return []
    records = [
        CalibrationRecord.model_validate_json(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]
    return records


def append_calibration(project: Project, record: CalibrationRecord) -> None:
    """Append one review to the calibration trail (creates the file/dir on first write)."""
    path = calibration_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(record.model_dump_json() + "\n")


def _latest_per_row(records: list[CalibrationRecord]) -> list[CalibrationRecord]:
    """Keep only the last ruling for each ``(run_id, question_id)``, in first-seen order.

    A human who re-opens and re-resolves a row appends a fresh record (the JSONL stays an
    append-only audit trail), but only their latest call should count — otherwise a repeated
    click would double-count in the agreement rate and duplicate a few-shot anchor."""
    latest: dict[tuple[str, str], CalibrationRecord] = {}
    for r in records:
        latest[(r.run_id, r.question_id)] = r  # later write wins
    return list(latest.values())


def judge_agreement(project: Project, *, split: str | None = None) -> JudgeAgreement:
    """The live judge↔human agreement, deduped to each row's latest ruling (spec §7).

    Optionally scoped to one split. Selection-biased by construction — see
    :class:`~sqbyl.models.JudgeAgreement`."""
    records = load_calibration(project)
    if split is not None:
        records = [r for r in records if r.split == split]
    return JudgeAgreement.from_records(_latest_per_row(records))


# How many prior human rulings to replay to the judge as few-shot examples. Small: a few
# concrete anchors coach without ballooning every judge prompt (and cost).
FEW_SHOT_LIMIT = 3


def few_shot_examples(
    project: Project, *, split: str, limit: int = FEW_SHOT_LIMIT
) -> list[CalibrationRecord]:
    """The most recent human rulings **for one split**, to coach that split's judge (spec §7).

    Split-scoped so dev rulings never coach the held-out test judge (invariant 3), and deduped
    to each row's latest ruling. Recency, not cherry-picking: the last ``limit`` reviews (both
    agreements and overrides) so the judge sees honest ground-truth anchors, not a
    disagreement-biased sample. Empty until a human has reviewed something on this split — so
    an un-reviewed project's judge prompts, and the CI cassettes, are unchanged."""
    records = [r for r in load_calibration(project) if r.split == split]
    return _latest_per_row(records)[-limit:]
