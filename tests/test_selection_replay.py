"""Record-replay coverage for the LLM-selection path (plan 9.1, invariant 4).

The large-schema path makes *two* LLM calls per ``ask()``: a table-shortlisting call
then the generate call. This captures both into one cassette (keyed by each request's
real fingerprint) and replays them in CI with zero tokens — proving selection usage is
metered and both calls round-trip deterministically.

Regenerate after an intentional prompt/context change:  SQBYL_UPDATE_CASSETTES=1
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from sqbyl.project import Project
from sqbyl.projectfiles import load_knowledge
from sqbyl_runtime.context import ProjectKnowledge
from sqbyl_runtime.db import Database
from sqbyl_runtime.llm.base import LLMResponse, Usage
from sqbyl_runtime.llm.mock import MockLLMClient
from sqbyl_runtime.llm.replay import RecordReplayLLMClient
from sqbyl_runtime.models import Dialect, SelectionConfig
from sqbyl_runtime.pipeline import ask

_CASSETTE = Path(__file__).resolve().parent / "cassettes" / "select_then_ask.json"
_QUESTION = "How many orders are there in total?"
_MODEL = "claude-opus-4-8"

# Call 1: the shortlister keeps only the orders table.
_SELECT_RESPONSE = LLMResponse(
    model=_MODEL,
    structured={"tables": ["analytics.orders"]},
    stop_reason="tool_use",
    usage=Usage(input_tokens=300, output_tokens=15),
)
# Call 2: generate, against the narrowed context.
_GEN_RESPONSE = LLMResponse(
    model=_MODEL,
    structured={
        "plan": "Count every row in the orders table.",
        "sql": "SELECT COUNT(*) AS total_orders FROM analytics.orders",
        "used_assets": [],
    },
    stop_reason="tool_use",
    usage=Usage(input_tokens=900, output_tokens=40),
)


def _llm_knowledge(dogfood_dir: Path) -> ProjectKnowledge:
    knowledge = load_knowledge(Project.load(dogfood_dir))
    return knowledge.model_copy(update={"selection": SelectionConfig(strategy="llm")})


def _write_cassette(knowledge: ProjectKnowledge, db: Database) -> None:
    capture = MockLLMClient([_SELECT_RESPONSE, _GEN_RESPONSE])
    ask(_QUESTION, knowledge=knowledge, db=db, llm=capture, model=_MODEL)
    entries = {}
    for request, response in zip(capture.requests, capture.responses, strict=True):
        entries[request.fingerprint()] = {
            "request": request.model_dump(mode="json"),
            "response": response.model_dump(mode="json"),
        }
    payload = {"version": 1, "entries": entries}
    _CASSETTE.parent.mkdir(parents=True, exist_ok=True)
    _CASSETTE.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def test_select_then_ask_replays_both_calls(dogfood_dir: Path, duckdb_path: Path) -> None:
    knowledge = _llm_knowledge(dogfood_dir)
    if os.environ.get("SQBYL_UPDATE_CASSETTES") or not _CASSETTE.exists():
        with Database.connect(str(duckdb_path), dialect=Dialect.duckdb) as db:
            _write_cassette(knowledge, db)

    client = RecordReplayLLMClient(_CASSETTE, mode="replay")
    with Database.connect(str(duckdb_path), dialect=Dialect.duckdb) as db:
        result = ask(_QUESTION, knowledge=knowledge, db=db, llm=client, model=_MODEL)

    assert result.ok
    assert result.selected_tables == ["analytics.orders"]  # selection narrowed to one
    assert result.rows == [[2000]]
    # Both calls' usage is folded into the run total: selection (300) + generate (900).
    assert result.usage.input_tokens == 1200
