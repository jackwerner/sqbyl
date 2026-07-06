"""End-to-end: the runtime ask() pipeline driven by the OpenAI client (fake SDK, no network).

Proves an OpenAI-backed provider produces a correct, executed answer through the full
generate -> validate -> execute -> respond path — not just that the client parses a reply in
isolation. The record-replay seam is provider-agnostic, so no new cassette is needed here.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sqbyl.project import Project
from sqbyl.projectfiles import load_knowledge
from sqbyl_runtime.context import ProjectKnowledge
from sqbyl_runtime.db import Database
from sqbyl_runtime.llm.openai_client import OpenAILLMClient
from sqbyl_runtime.models import Dialect
from sqbyl_runtime.pipeline import ask
from sqbyl_runtime.state.traces import TraceWriter


class _FakeCompletions:
    """Returns a forced-function reply carrying a valid counting query for every call."""

    def create(self, **kwargs: Any) -> Any:
        args = json.dumps(
            {
                "plan": "count the orders",
                "sql": "SELECT COUNT(*) AS n FROM analytics.orders",
                "used_assets": [],
            }
        )
        return SimpleNamespace(
            model=kwargs["model"],
            choices=[
                SimpleNamespace(
                    finish_reason="tool_calls",
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                function=SimpleNamespace(name="emit_result", arguments=args)
                            )
                        ],
                    ),
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=12, completion_tokens=6, prompt_tokens_details=None
            ),
        )


class _FakeOpenAISDK:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions())


@pytest.fixture
def knowledge(dogfood_dir: Path) -> ProjectKnowledge:
    return load_knowledge(Project.load(dogfood_dir))


def test_openai_client_answers_end_to_end(
    knowledge: ProjectKnowledge, duckdb_path: Path, tmp_path: Path
) -> None:
    db = Database.connect(str(duckdb_path), dialect=Dialect.duckdb)
    llm = OpenAILLMClient(client=_FakeOpenAISDK())
    result = ask(
        "How many orders are there?",
        knowledge=knowledge,
        db=db,
        llm=llm,
        model="gpt-5",
        trace_writer=TraceWriter(tmp_path / "traces.jsonl"),
    )
    db.close()

    assert result.ok
    assert result.rows and result.rows[0][0] > 0
    # Usage flowed through so the cost meter can price a gpt-5 run.
    assert result.usage.output_tokens == 6
