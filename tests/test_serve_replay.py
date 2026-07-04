"""Record-replay coverage for the `sqbyl serve` path (plan 9.2, invariant 4).

Every LLM-touching path ships at least one record-replay fixture. `pipeline.ask` is
covered by `test_pipeline_replay.py`; this covers the *serve* wiring on top of it —
`project_endpoint` → `ChatServer.ask` → per-call metering to `.sqbyl/usage.db` — driven
by a replayed cassette so it runs in CI with zero tokens.

Regenerate after an intentional prompt/context change:  SQBYL_UPDATE_CASSETTES=1
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sqbyl.project import Project
from sqbyl.serve import ChatServer, project_endpoint
from sqbyl_runtime.llm.base import LLMResponse, Usage
from sqbyl_runtime.llm.mock import MockLLMClient
from sqbyl_runtime.llm.replay import RecordReplayLLMClient
from sqbyl_runtime.state.layout import SqbylPaths

_CASSETTE = Path(__file__).resolve().parent / "cassettes" / "serve_ask.json"
_QUESTION = "How many orders are there in total?"
_RESPONSE = LLMResponse(
    model="claude-opus-4-8",
    structured={
        "plan": "Count every row in the orders table.",
        "sql": "SELECT COUNT(*) AS total_orders FROM analytics.orders",
        "used_assets": [],
    },
    stop_reason="tool_use",
    usage=Usage(input_tokens=1100, output_tokens=40),
)


def _write_cassette(project: Project, tmp: Path) -> None:
    capture = MockLLMClient([_RESPONSE])
    chat = ChatServer(project_endpoint(project, llm=capture), paths=SqbylPaths(tmp))
    chat.ask(_QUESTION)
    chat.close()
    entries = {
        req.fingerprint(): {
            "request": req.model_dump(mode="json"),
            "response": resp.model_dump(mode="json"),
        }
        for req, resp in zip(capture.requests, capture.responses, strict=True)
    }
    _CASSETTE.parent.mkdir(parents=True, exist_ok=True)
    _CASSETTE.write_text(json.dumps({"version": 1, "entries": entries}, indent=2) + "\n")


def test_serve_ask_replays_from_cassette(
    dogfood_dir: Path, duckdb_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))
    project = Project.load(dogfood_dir)
    if os.environ.get("SQBYL_UPDATE_CASSETTES") or not _CASSETTE.exists():
        _write_cassette(project, tmp_path / "capture")

    client = RecordReplayLLMClient(_CASSETTE, mode="replay")
    chat = ChatServer(project_endpoint(project, llm=client), paths=SqbylPaths(tmp_path))
    try:
        data = chat.ask(_QUESTION)
    finally:
        chat.close()

    assert data["ok"] is True
    assert data["rows"] == [[2000]]
    assert data["cost_usd"] > 0  # the replayed usage was priced and metered
    assert data["tokens"] == 1140  # 1100 input + 40 output from the recorded response
