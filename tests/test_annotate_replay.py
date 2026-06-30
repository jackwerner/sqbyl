"""Phase 2.3 — record-replay coverage for the annotator (invariant 4).

Like the pipeline cassette: capture the exact request ``annotate_table`` emits,
pair it with a canned response, and replay it with no key.

Regenerate after an intentional prompt change:  SQBYL_UPDATE_CASSETTES=1
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sqbyl.annotate import TableAnnotation, annotate_table
from sqbyl.introspect import introspect
from sqbyl.profile import profile_table
from sqbyl_runtime.db import Database
from sqbyl_runtime.llm.base import LLMResponse, Usage
from sqbyl_runtime.llm.mock import MockLLMClient
from sqbyl_runtime.llm.replay import RecordReplayLLMClient
from sqbyl_runtime.models import Dialect, TableSemantics

_CASSETTE = Path(__file__).resolve().parent / "cassettes" / "annotate_orders.json"
_RESPONSE = LLMResponse(
    model="claude-opus-4-8",
    structured={
        "description": "One row per order placed by a customer.",
        "synonyms": ["purchases", "sales"],
        "confidence": 0.92,
        "columns": [
            {"name": "amount_cents", "description": "Order total in cents.", "confidence": 0.95},
        ],
    },
    stop_reason="tool_use",
    usage=Usage(input_tokens=900, output_tokens=120, cache_creation_input_tokens=800),
)


@pytest.fixture
def profiled_orders(duckdb_path: Path) -> TableSemantics:
    with Database.connect(str(duckdb_path), dialect=Dialect.duckdb) as db:
        orders = next(t for t in introspect(db) if t.table == "analytics.orders")
        return profile_table(db, orders)


def _write_cassette(table: TableSemantics) -> None:
    capture = MockLLMClient([_RESPONSE])
    annotate_table(capture, table, model="claude-opus-4-8")
    request = capture.requests[0]
    payload = {
        "version": 1,
        "entries": {
            request.fingerprint(): {
                "request": request.model_dump(mode="json"),
                "response": _RESPONSE.model_dump(mode="json"),
            }
        },
    }
    _CASSETTE.parent.mkdir(parents=True, exist_ok=True)
    _CASSETTE.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def test_annotate_replays_from_cassette(profiled_orders: TableSemantics) -> None:
    if os.environ.get("SQBYL_UPDATE_CASSETTES") or not _CASSETTE.exists():
        _write_cassette(profiled_orders)

    client = RecordReplayLLMClient(_CASSETTE, mode="replay")
    annotation, response = annotate_table(client, profiled_orders, model="claude-opus-4-8")

    assert isinstance(annotation, TableAnnotation)
    assert annotation.description.startswith("One row per order")
    assert annotation.confidence == 0.92
    assert response.usage.input_tokens == 900
