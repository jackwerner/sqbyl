"""Phase 2.2 — record-replay coverage for ``ask()`` (invariant 4).

Every LLM-touching path ships at least one record-replay fixture that runs in CI
with zero tokens. The cassette here is keyed by the pipeline's *real* request
fingerprint: we capture the exact request ``ask()`` emits, pair it with a canned
response, and then replay it through ``RecordReplayLLMClient`` — no network, no key.

Regenerate after an intentional prompt/context change:  SQBYL_UPDATE_CASSETTES=1
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sqbyl.project import Project
from sqbyl.projectfiles import load_knowledge
from sqbyl_runtime.context import ProjectKnowledge
from sqbyl_runtime.db import Database
from sqbyl_runtime.llm.base import LLMResponse, Usage
from sqbyl_runtime.llm.mock import MockLLMClient
from sqbyl_runtime.llm.replay import RecordReplayLLMClient
from sqbyl_runtime.models import Dialect
from sqbyl_runtime.pipeline import ask

_CASSETTE = Path(__file__).resolve().parent / "cassettes" / "ask_total_orders.json"
_QUESTION = "How many orders are there in total?"
_RESPONSE = LLMResponse(
    model="claude-opus-4-8",
    structured={
        "plan": "Count every row in the orders table.",
        "sql": "SELECT COUNT(*) AS total_orders FROM analytics.orders",
        "used_assets": [],
    },
    stop_reason="tool_use",
    usage=Usage(input_tokens=1200, output_tokens=40, cache_creation_input_tokens=1100),
)


@pytest.fixture
def knowledge(dogfood_dir: Path) -> ProjectKnowledge:
    return load_knowledge(Project.load(dogfood_dir))


def _write_cassette(knowledge: ProjectKnowledge, db: Database) -> None:
    """Capture the exact request ask() emits and store it keyed by fingerprint."""
    capture = MockLLMClient([_RESPONSE])
    ask(_QUESTION, knowledge=knowledge, db=db, llm=capture, model="claude-opus-4-8")
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


def test_ask_replays_from_cassette(knowledge: ProjectKnowledge, duckdb_path: Path) -> None:
    if os.environ.get("SQBYL_UPDATE_CASSETTES") or not _CASSETTE.exists():
        with Database.connect(str(duckdb_path), dialect=Dialect.duckdb) as db:
            _write_cassette(knowledge, db)

    client = RecordReplayLLMClient(_CASSETTE, mode="replay")
    with Database.connect(str(duckdb_path), dialect=Dialect.duckdb) as db:
        result = ask(_QUESTION, knowledge=knowledge, db=db, llm=client, model="claude-opus-4-8")

    assert result.ok
    assert result.sql == "SELECT COUNT(*) AS total_orders FROM analytics.orders"
    assert result.columns == ["total_orders"]
    assert result.rows == [[2000]]
    # Usage from the recorded response flows through (cache tokens included).
    assert result.usage.input_tokens == 1200
    assert result.usage.cache_creation_input_tokens == 1100
