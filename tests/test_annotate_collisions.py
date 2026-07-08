"""Finding #2 — deterministic ($0) synonym-collision detection in the annotator.

The per-table annotator can confidently give one column a synonym that equally describes a
sibling (the classic ``cost``/``cost_price`` vs ``unit_price`` trap). These tests cover the
detector, the confidence cap, and the written-file scan `init` uses.
"""

from __future__ import annotations

from sqbyl.annotate import (
    ColumnAnnotation,
    TableAnnotation,
    detect_semantics_collisions,
    detect_synonym_collisions,
    flag_synonym_collisions,
)
from sqbyl_runtime.models import Column, TableSemantics


def _annotation(columns: list[ColumnAnnotation]) -> TableAnnotation:
    return TableAnnotation(description="products", synonyms=[], confidence=0.9, columns=columns)


def _col(name: str, synonyms: list[str], confidence: float = 0.9) -> ColumnAnnotation:
    return ColumnAnnotation(name=name, description="d", synonyms=synonyms, confidence=confidence)


def test_detects_the_cost_vs_unit_price_collision() -> None:
    annotation = _annotation(
        [
            _col("cost_price", ["cost", "purchase price", "acquisition cost", "COGS"]),
            _col("unit_price", ["price", "sale price", "unit price"]),
        ]
    )
    collisions = detect_synonym_collisions(annotation)
    tokens = {c.token for c in collisions}
    assert "price" in tokens
    pair = next(c for c in collisions if c.token == "price").columns
    assert pair == ("cost_price", "unit_price")
    assert "shared vocabulary" in collisions[0].describe()


def test_no_collision_for_unrelated_columns() -> None:
    annotation = _annotation(
        [
            _col("customer_id", ["customer", "buyer"]),
            _col("created_at", ["created", "signup date"]),
        ]
    )
    assert detect_synonym_collisions(annotation) == []


def test_stopwords_and_short_tokens_do_not_collide() -> None:
    # "amount" is a generic stopword; "id" is too short/generic — neither should collide.
    annotation = _annotation(
        [
            _col("net_amount", ["amount", "net"]),
            _col("gross_amount", ["amount", "gross"]),
        ]
    )
    tokens = {c.token for c in detect_synonym_collisions(annotation)}
    assert "amount" not in tokens  # generic, filtered


def test_flag_caps_contested_column_confidence_below_auto_apply() -> None:
    annotation = _annotation(
        [
            _col("cost_price", ["cost", "purchase price"], confidence=0.95),
            _col("unit_price", ["price", "sale price"], confidence=0.95),
            _col("sku", ["item code"], confidence=0.95),
        ]
    )
    flagged, collisions = flag_synonym_collisions(annotation)
    assert collisions
    by_name = {c.name: c for c in flagged.columns}
    assert by_name["cost_price"].confidence <= 0.5  # capped
    assert by_name["unit_price"].confidence <= 0.5  # capped
    assert by_name["sku"].confidence == 0.95  # uncontested, untouched


def test_no_collision_returns_annotation_unchanged() -> None:
    annotation = _annotation([_col("sku", ["item code"], confidence=0.9)])
    flagged, collisions = flag_synonym_collisions(annotation)
    assert collisions == []
    assert flagged is annotation


def test_detect_semantics_collisions_on_a_written_table() -> None:
    table = TableSemantics(
        table="products",
        columns=[
            Column(name="cost_price", type="numeric", synonyms=["cost", "purchase price"]),
            Column(name="unit_price", type="numeric", synonyms=["price", "sale price"]),
        ],
    )
    tokens = {c.token for c in detect_semantics_collisions(table)}
    assert "price" in tokens


def test_table_name_root_is_not_a_collision() -> None:
    # The noise case (UX finding #2): every column in an orders table naturally shares the
    # entity root "order" — order_id's "order number", order_date's "order date". That's the
    # table's subject, not a real ambiguity, so it must not be flagged (de-pluralized too).
    table = TableSemantics(
        table="analytics.orders",
        columns=[
            Column(name="order_id", type="bigint", synonyms=["order number", "order identifier"]),
            Column(name="order_date", type="date", synonyms=["order date", "date ordered"]),
        ],
    )
    assert detect_semantics_collisions(table) == []


def test_topical_root_excluded_but_real_contest_survives() -> None:
    # "product" is topical (the table's own entity) and must be dropped; "price" is a genuine
    # contest between two sibling price columns and must survive — the signal, not the noise.
    table = TableSemantics(
        table="analytics.products",
        columns=[
            Column(name="product_id", type="bigint", synonyms=["product identifier"]),
            Column(name="product_name", type="text", synonyms=["product", "name"]),
            Column(name="cost_price", type="numeric", synonyms=["cost", "purchase price"]),
            Column(name="unit_price", type="numeric", synonyms=["price", "sale price"]),
        ],
    )
    tokens = {c.token for c in detect_semantics_collisions(table)}
    assert "product" not in tokens  # topical, dropped
    assert tokens == {"price"}  # only the real contest remains


def test_identifier_is_a_generic_id_word_not_a_collision() -> None:
    # Two ID columns both carrying "...identifier" synonyms shouldn't collide on it — it's the
    # long form of the already-excluded "id", never the disambiguation a human needs.
    table = TableSemantics(
        table="analytics.line_items",
        columns=[
            Column(name="order_id", type="bigint", synonyms=["order identifier"]),
            Column(name="product_id", type="bigint", synonyms=["product identifier"]),
        ],
    )
    tokens = {c.token for c in detect_semantics_collisions(table)}
    assert "identifier" not in tokens


def test_detect_synonym_collisions_excludes_topical_when_given_the_table_name() -> None:
    # The draft-time entry point (the annotation object has no table name) takes it explicitly.
    annotation = _annotation(
        [
            _col("order_id", ["order number"]),
            _col("order_date", ["order date"]),
        ]
    )
    assert detect_synonym_collisions(annotation, table_name="orders") == []
    # Without the name it can't know "order" is topical, so it still flags it (backward-compatible).
    assert any(c.token == "order" for c in detect_synonym_collisions(annotation))
