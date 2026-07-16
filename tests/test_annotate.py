"""Phase 2.3 — the annotator (spec §3 #1).

Exit criterion: annotating the (stripped) fixture produces sensible descriptions
grounded in the profile; confidence is populated; profiling is preserved. Zero-token
(mock seam).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sqbyl.annotate import annotate_table, apply_annotation
from sqbyl.introspect import introspect
from sqbyl.profile import profile_table
from sqbyl.semantics_io import merge_annotation
from sqbyl.yamlio import dump_yaml, load_yaml
from sqbyl_runtime.db import Database
from sqbyl_runtime.llm.mock import MockLLMClient, structured_reply
from sqbyl_runtime.models import Dialect, TableSemantics


@pytest.fixture
def profiled_orders(duckdb_path: Path) -> TableSemantics:
    with Database.connect(str(duckdb_path), dialect=Dialect.duckdb) as db:
        orders = next(t for t in introspect(db) if t.table == "analytics.orders")
        return profile_table(db, orders)


_REPLY = structured_reply(
    {
        "description": "One row per order.",
        "synonyms": ["purchases", "sales"],
        "confidence": 0.9,
        "columns": [
            {"name": "order_id", "description": "Primary key.", "synonyms": [], "confidence": 0.99},
            {
                "name": "amount_cents",
                "description": "Order total in cents (divide by 100 for dollars).",
                "synonyms": ["amount"],
                "confidence": 0.95,
            },
            {
                "name": "status",
                "description": "Order lifecycle status.",
                "synonyms": ["state"],
                "confidence": 0.8,
            },
        ],
    }
)


def test_prompt_is_grounded_in_profile(profiled_orders: TableSemantics) -> None:
    llm = MockLLMClient([_REPLY])
    annotate_table(llm, profiled_orders, model="claude-opus-4-8")
    prompt = llm.requests[0].messages[0].content
    # The profile reaches the model: numeric range and categorical values, not just names.
    assert "range=127..310229" in prompt
    assert "confirmed" in prompt  # status sample values
    assert llm.requests[0].system is not None and "grounded" in llm.requests[0].system


def test_numeric_text_magnitude_reaches_the_prompt() -> None:
    # UX risk 1: for A4 the flag alone doesn't disambiguate "population" from "area" — the
    # *magnitude* does. A high-cardinality numbers-as-text column must render its numeric range
    # (and an existing catalog note, if any) into the prompt the model actually sees.
    from sqbyl_runtime.models import Column, Profile, TableSemantics

    table = TableSemantics(
        table="bird.district",
        columns=[
            Column(
                name="A4",
                type="text",
                profile=Profile(distinct=1200, numeric_text=True, min=52, max=1_200_000),
            )
        ],
    )
    llm = MockLLMClient([_REPLY])
    annotate_table(llm, table, model="m")
    prompt = llm.requests[0].messages[0].content
    assert "range=52..1200000" in prompt  # the magnitude, not just the flag
    assert "stored as text but values are numeric" in prompt


def test_accepts_nested_table_description_wrapper() -> None:
    # Finding B9: claude-sonnet-5 sometimes wraps the annotation in a single object field
    # instead of the flat shape. TableAnnotation unwraps it rather than raising.
    from sqbyl.annotate import TableAnnotation

    wrapped = {
        "table_description": {
            "description": "One row per loan.",
            "confidence": 0.88,
            "columns": [{"name": "loan_state", "description": "d", "confidence": 0.7}],
        }
    }
    ann = TableAnnotation.model_validate(wrapped)
    assert ann.description == "One row per loan." and ann.confidence == 0.88
    assert ann.columns[0].name == "loan_state"
    # A well-formed flat payload is untouched (the guard only fires when description is absent).
    flat = TableAnnotation.model_validate({"description": "d", "confidence": 0.5})
    assert flat.description == "d"


def test_annotate_table_parses_the_wrapped_shape(profiled_orders: TableSemantics) -> None:
    wrapped = structured_reply(
        {
            "table_annotation": {
                "description": "One row per order.",
                "confidence": 0.9,
                "columns": [{"name": "order_id", "description": "pk", "confidence": 0.99}],
            }
        }
    )
    annotation, _ = annotate_table(MockLLMClient([wrapped]), profiled_orders, model="m")
    assert annotation.description == "One row per order."
    assert annotation.columns[0].name == "order_id"


def test_confidence_is_populated(profiled_orders: TableSemantics) -> None:
    annotation, _ = annotate_table(MockLLMClient([_REPLY]), profiled_orders, model="m")
    assert annotation.confidence == 0.9
    assert {c.name: c.confidence for c in annotation.columns} == {
        "order_id": 0.99,
        "amount_cents": 0.95,
        "status": 0.8,
    }


def test_apply_writes_descriptions_keeps_profile(profiled_orders: TableSemantics) -> None:
    annotation, _ = annotate_table(MockLLMClient([_REPLY]), profiled_orders, model="m")
    applied = apply_annotation(profiled_orders, annotation)
    assert applied.description == "One row per order."
    assert applied.synonyms == ["purchases", "sales"]
    by_name = {c.name: c for c in applied.columns}
    assert by_name["amount_cents"].description.startswith("Order total in cents")
    # Profiling survives annotation (descriptions are additive).
    assert all(c.profile is not None for c in applied.columns)
    assert by_name["status"].sample_values == ["confirmed", "partial_refund", "refunded"]


def test_merge_preserves_profile_blocks_in_yaml(
    profiled_orders: TableSemantics, tmp_path: Path
) -> None:
    # Round-trip through YAML the way the CLI writes it back.
    raw = load_yaml(dump_yaml(profiled_orders.model_dump(exclude_none=True, exclude_defaults=True)))
    annotation, _ = annotate_table(MockLLMClient([_REPLY]), profiled_orders, model="m")
    merged = merge_annotation(raw, annotation)
    by_name = {c["name"]: c for c in merged["columns"]}
    assert merged["description"] == "One row per order."
    assert by_name["amount_cents"]["description"].startswith("Order total")
    # The profile block written by `sqbyl profile` is untouched by annotation.
    assert by_name["amount_cents"]["profile"]["min"] == 127
    assert by_name["status"]["sample_values"] == ["confirmed", "partial_refund", "refunded"]
