"""Record-replay coverage for narrated ``ask()`` (invariant 4).

The narration path is LLM-touching, so it ships at least one cassette that runs in CI
with zero tokens. The cassette holds both calls a narrated run makes — the structured
*generate* and the free-text *narrate* — each keyed by the pipeline's real request
fingerprint, so replay exercises the true wiring, not a stub.

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

_CASSETTE = Path(__file__).resolve().parent / "cassettes" / "ask_narrate_total_orders.json"
_QUESTION = "How many orders are there in total?"
_MODEL = "claude-opus-4-8"
_GENERATE = LLMResponse(
    model=_MODEL,
    structured={
        "plan": "Count every row in the orders table.",
        "sql": "SELECT COUNT(*) AS total_orders FROM analytics.orders",
        "used_assets": [],
    },
    stop_reason="tool_use",
    usage=Usage(input_tokens=1200, output_tokens=40, cache_creation_input_tokens=1100),
)
_NARRATE = LLMResponse(
    model=_MODEL,
    text="There are 2,000 orders in total.",
    stop_reason="end_turn",
    usage=Usage(input_tokens=90, output_tokens=12),
)


@pytest.fixture
def knowledge(dogfood_dir: Path) -> ProjectKnowledge:
    return load_knowledge(Project.load(dogfood_dir))


def _write_cassette(knowledge: ProjectKnowledge, db: Database) -> None:
    """Capture the exact generate+narrate requests ask() emits, keyed by fingerprint."""
    capture = MockLLMClient([_GENERATE, _NARRATE])
    ask(_QUESTION, knowledge=knowledge, db=db, llm=capture, model=_MODEL, narrate=True)
    entries = {
        req.fingerprint(): {
            "request": req.model_dump(mode="json"),
            "response": resp.model_dump(mode="json"),
        }
        for req, resp in zip(capture.requests, capture.responses, strict=True)
    }
    _CASSETTE.parent.mkdir(parents=True, exist_ok=True)
    _CASSETTE.write_text(
        json.dumps({"version": 1, "entries": entries}, indent=2, sort_keys=True) + "\n"
    )


def test_narrated_ask_replays_from_cassette(knowledge: ProjectKnowledge, duckdb_path: Path) -> None:
    if os.environ.get("SQBYL_UPDATE_CASSETTES") or not _CASSETTE.exists():
        with Database.connect(str(duckdb_path), dialect=Dialect.duckdb) as db:
            _write_cassette(knowledge, db)

    client = RecordReplayLLMClient(_CASSETTE, mode="replay")
    with Database.connect(str(duckdb_path), dialect=Dialect.duckdb) as db:
        result = ask(_QUESTION, knowledge=knowledge, db=db, llm=client, model=_MODEL, narrate=True)

    assert result.ok
    assert result.rows == [[2000]]  # the authoritative answer
    assert result.answer == "There are 2,000 orders in total."  # the convenience sentence
    # Narration tokens are metered apart from the agent spend.
    assert result.narration_usage.output_tokens == 12
    assert result.usage.cache_creation_input_tokens == 1100
