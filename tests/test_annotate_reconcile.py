"""Finding B11 — `annotate` reconciles instead of disposing.

The build step must keep the same honesty bar as the rest of sqbyl: never silently overwrite
an authoritative (catalog/human) description, and route what it can't ground confidently to the
review queue rather than asserting a guess. These cover the reconcile policy, the fill-only
merge mechanism, and the persisted review proposals the console surfaces.
"""

from __future__ import annotations

from pathlib import Path

from sqbyl.annotate import (
    ColumnAnnotation,
    TableAnnotation,
    apply_annotation,
    load_annotation_review,
    reconcile_annotation,
    save_annotation_review,
)
from sqbyl.models.attention import DecisionKind
from sqbyl.semantics_io import merge_annotation
from sqbyl_runtime.models import Column, Profile, TableSemantics


def _table(columns: list[Column], *, description: str | None = None) -> TableSemantics:
    return TableSemantics(table="analytics.orders", description=description, columns=columns)


def _draft(
    columns: list[ColumnAnnotation], *, description: str = "d", confidence: float = 0.95
) -> TableAnnotation:
    return TableAnnotation(description=description, confidence=confidence, columns=columns)


def test_existing_note_is_kept_and_not_queued() -> None:
    # An authoritative note is preserved; the model's re-draft is dropped, no review card.
    table = _table([Column(name="a4", type="text", description="Population of the district.")])
    draft = _draft([ColumnAnnotation(name="a4", description="Area in acres.", confidence=0.99)])
    safe, decisions = reconcile_annotation(table, draft, threshold=0.85)
    # The draft for a described column is blanked so merge keeps the note.
    assert safe.columns[0].description == ""
    assert decisions == []  # trusting the note over the model → no noise


def test_low_confidence_undescribed_column_is_withheld_and_queued() -> None:
    table = _table([Column(name="cryptic", type="text")])  # no description yet
    draft = _draft([ColumnAnnotation(name="cryptic", description="A guess.", confidence=0.4)])
    safe, decisions = reconcile_annotation(table, draft, threshold=0.85)
    assert safe.columns[0].description == ""  # not written as truth
    assert len(decisions) == 1
    d = decisions[0]
    assert d.kind is DecisionKind.column_description
    assert d.suggestion == "A guess." and d.confidence == 0.4
    assert d.source == "annotate:analytics.orders"


def test_confident_undescribed_column_is_applied() -> None:
    table = _table([Column(name="total", type="numeric")])
    draft = _draft([ColumnAnnotation(name="total", description="Order total.", confidence=0.97)])
    safe, decisions = reconcile_annotation(table, draft, threshold=0.85)
    assert safe.columns[0].description == "Order total."  # confident fill kept
    assert decisions == []


def test_confident_numeric_text_column_is_still_withheld_with_its_range() -> None:
    # The A4 case (UX risk 1): a text column that's actually numeric mislabels *confidently*
    # ("Total area, likely km²" at 0.99). Its type mismatch must earn review even when the
    # model is sure — and the card must carry the magnitude that exposes the guess as wrong.
    col = Column(name="a4", type="text", profile=Profile(numeric_text=True, min=52, max=1_200_000))
    table = _table([col])
    draft = _draft(
        [ColumnAnnotation(name="a4", description="Total area, likely km².", confidence=0.99)]
    )
    safe, decisions = reconcile_annotation(table, draft, threshold=0.85)
    assert safe.columns[0].description == ""  # confident-but-wrong guess NOT auto-applied
    assert len(decisions) == 1
    assert "numbers stored as text" in decisions[0].detail
    assert "52..1200000" in decisions[0].detail  # the magnitude reaches the reviewer


def test_low_confidence_numeric_text_card_notes_the_cast() -> None:
    col = Column(name="a4", type="text", profile=Profile(numeric_text=True))
    table = _table([col])
    draft = _draft([ColumnAnnotation(name="a4", description="?", confidence=0.3)])
    _, decisions = reconcile_annotation(table, draft, threshold=0.85)
    assert "numbers stored as text" in decisions[0].detail


def test_withhold_count_is_bounded_by_genuinely_ungrounded_columns() -> None:
    # UX risk 2: the queue must not flood. Withholds are proportional to columns that genuinely
    # can't be grounded (below threshold) plus numeric-text ones — a confident, described, or
    # plain-typed column never queues. Here: 3 low-confidence + 1 numeric-text = 4 of 10.
    columns = [Column(name=f"ok{i}", type="text") for i in range(6)]
    columns += [Column(name=f"lo{i}", type="text") for i in range(3)]
    columns.append(Column(name="code", type="text", profile=Profile(numeric_text=True)))
    drafts = [
        ColumnAnnotation(name=f"ok{i}", description="clear", confidence=0.95) for i in range(6)
    ]
    drafts += [
        ColumnAnnotation(name=f"lo{i}", description="guess", confidence=0.4) for i in range(3)
    ]
    drafts.append(ColumnAnnotation(name="code", description="a code", confidence=0.95))
    _, decisions = reconcile_annotation(_table(columns), _draft(drafts), threshold=0.85)
    assert len(decisions) == 4  # 6 confident columns auto-apply; only the 4 genuine gaps queue


def test_merge_is_fill_only_and_unions_synonyms() -> None:
    raw = {
        "table": "analytics.orders",
        "description": "Human-authored: one row per order.",
        "columns": [
            {"name": "a4", "description": "Population.", "synonyms": ["people"]},
            {"name": "total", "synonyms": []},  # blank description
        ],
    }
    draft = _draft(
        [
            ColumnAnnotation(
                name="a4", description="Area.", synonyms=["headcount"], confidence=0.9
            ),
            ColumnAnnotation(name="total", description="Order total.", confidence=0.9),
        ],
        description="Machine draft.",
    )
    out = merge_annotation(raw, draft)
    # Non-empty descriptions survive; only the blank one is filled.
    assert out["description"] == "Human-authored: one row per order."
    a4 = next(c for c in out["columns"] if c["name"] == "a4")
    total = next(c for c in out["columns"] if c["name"] == "total")
    assert a4["description"] == "Population."  # not overwritten
    assert total["description"] == "Order total."  # blank filled
    # Synonyms are unioned (existing first), not replaced.
    assert a4["synonyms"] == ["people", "headcount"]


def test_apply_annotation_is_fill_only() -> None:
    table = _table([Column(name="a4", type="text", description="Population.", synonyms=["people"])])
    draft = _draft(
        [ColumnAnnotation(name="a4", description="Area.", synonyms=["headcount"], confidence=0.9)]
    )
    applied = apply_annotation(table, draft)
    assert applied.columns[0].description == "Population."  # kept
    assert applied.columns[0].synonyms == ["people", "headcount"]  # unioned


def test_review_proposals_round_trip(tmp_path: Path) -> None:
    table = _table([Column(name="cryptic", type="text")])
    draft = _draft([ColumnAnnotation(name="cryptic", description="guess", confidence=0.2)])
    _, decisions = reconcile_annotation(table, draft, threshold=0.85)
    path = tmp_path / "annotate_review.json"
    save_annotation_review(path, decisions)
    loaded = load_annotation_review(path)
    assert [d.id for d in loaded] == [d.id for d in decisions]
    assert load_annotation_review(tmp_path / "missing.json") == []  # absent → empty
