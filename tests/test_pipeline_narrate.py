"""Opt-in NL narration for ``ask()`` (finding #14).

Narration is off by default and, when enabled, adds exactly one grounded, separately
metered summarization call that populates ``result.answer`` without ever touching the
authoritative rows. All zero-token (mock seam, invariant 4).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sqbyl.project import Project
from sqbyl.projectfiles import load_knowledge
from sqbyl_runtime.context import ProjectKnowledge
from sqbyl_runtime.db import Database
from sqbyl_runtime.llm.base import Usage
from sqbyl_runtime.llm.mock import MockLLMClient, structured_reply, text_reply
from sqbyl_runtime.models import Dialect
from sqbyl_runtime.pipeline import ask
from sqbyl_runtime.state.traces import TraceWriter, read_spans

_COUNT_SQL = "SELECT COUNT(*) AS n FROM analytics.orders"


@pytest.fixture
def knowledge(dogfood_dir: Path) -> ProjectKnowledge:
    return load_knowledge(Project.load(dogfood_dir))


@pytest.fixture
def db(duckdb_path: Path) -> Database:
    with Database.connect(str(duckdb_path), dialect=Dialect.duckdb) as conn:
        yield conn


def _gen(sql: str = _COUNT_SQL) -> object:
    return structured_reply({"plan": "p", "sql": sql, "used_assets": []})


def test_narration_is_off_by_default(knowledge: ProjectKnowledge, db: Database) -> None:
    llm = MockLLMClient([_gen()])
    result = ask("count", knowledge=knowledge, db=db, llm=llm, model="m")
    # Deterministic, $0-by-default posture: exactly one call, no answer, no narration spend.
    assert result.answer is None
    assert result.narration_usage.total_tokens == 0
    assert llm.call_count == 1


def test_narrate_populates_answer_and_meters_separately(
    knowledge: ProjectKnowledge, db: Database
) -> None:
    llm = MockLLMClient(
        [
            _gen(),  # generate
            text_reply(
                "There are 2000 orders in total.", usage=Usage(input_tokens=40, output_tokens=8)
            ),
        ]
    )
    result = ask("count", knowledge=knowledge, db=db, llm=llm, model="m", narrate=True)
    assert result.ok
    assert result.answer == "There are 2000 orders in total."
    # The narration tokens land in their own bucket, apart from the agent spend...
    assert result.narration_usage.output_tokens == 8
    assert result.usage.output_tokens != 8 or result.usage.input_tokens != 40
    # ...and total_usage is the grand total of the two.
    assert result.total_usage.total_tokens == (
        result.usage.total_tokens + result.narration_usage.total_tokens
    )
    # Rows remain the source of truth — narration adds a field, never mutates the answer set.
    assert result.rows == [[2000]]
    assert llm.call_count == 2


def test_narration_prompt_is_grounded_on_the_executed_rows(
    knowledge: ProjectKnowledge, db: Database
) -> None:
    llm = MockLLMClient([_gen(), text_reply("2000 orders.")])
    ask("How many orders?", knowledge=knowledge, db=db, llm=llm, model="m", narrate=True)
    narrate_req = llm.requests[1]
    body = narrate_req.messages[-1].content
    # The question and the real result (columns + value) are both in the prompt, so the
    # sentence is anchored to executed output and can't invent a figure.
    assert "How many orders?" in body
    assert "n" in body and "2000" in body
    # It's a free-text call (no forced JSON schema), distinct from the agent's structured one.
    assert narrate_req.response_schema is None


def test_narration_is_skipped_when_the_query_fails(
    knowledge: ProjectKnowledge, db: Database
) -> None:
    # Every attempt fails static validation → nothing grounded to summarize.
    llm = MockLLMClient([_gen("SELECT nope FROM analytics.orders")] * 3)
    result = ask("bad", knowledge=knowledge, db=db, llm=llm, model="m", narrate=True)
    assert not result.ok
    assert result.answer is None
    assert result.narration_usage.total_tokens == 0
    # Only the (failed) generate attempts spent calls — no narration call was made.
    assert llm.call_count == 3


def test_narration_model_override_is_used(knowledge: ProjectKnowledge, db: Database) -> None:
    llm = MockLLMClient([_gen(), text_reply("2000 orders.")])
    ask(
        "count",
        knowledge=knowledge,
        db=db,
        llm=llm,
        model="agent-model",
        narrate=True,
        narration_model="cheap-narrator",
    )
    assert llm.requests[0].model == "agent-model"
    assert llm.requests[1].model == "cheap-narrator"


def test_narration_writes_its_own_child_span(
    knowledge: ProjectKnowledge, db: Database, tmp_path: Path
) -> None:
    writer = TraceWriter(tmp_path / "trace.jsonl")
    llm = MockLLMClient([_gen(), text_reply("2000 orders.")])
    result = ask(
        "count", knowledge=knowledge, db=db, llm=llm, model="m", narrate=True, trace_writer=writer
    )
    spans = read_spans(tmp_path / "trace.jsonl")
    run = next(s for s in spans if s.name == "ask")
    narrate = next(s for s in spans if s.name == "narrate")
    # Traced as a GenAI child of the run span (invariant 7), and the run notes it narrated.
    assert narrate.trace_id == result.trace_id
    assert narrate.parent_span_id == run.span_id
    assert narrate.attributes["gen_ai.operation.name"] == "chat"
    assert run.attributes["sqbyl.narrated"] is True


def test_empty_narration_completion_yields_none(knowledge: ProjectKnowledge, db: Database) -> None:
    # A blank/whitespace completion should read as "no narration", not an empty string.
    llm = MockLLMClient([_gen(), text_reply("   ")])
    result = ask("count", knowledge=knowledge, db=db, llm=llm, model="m", narrate=True)
    assert result.answer is None
