"""Phase 9.3 — export shapes of a loaded release (spec §10, plan 9.3).

The plain callable and the stdlib MCP server are exercised end-to-end against a real
loaded release (built here from the dogfood project) with a scripted MockLLMClient — no
tokens (invariant 4). The LangChain shape is tested at its seam (the missing-extra hint),
since langchain isn't a CI dependency.
"""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pytest

from sqbyl.eval.report import save_run
from sqbyl.models import QuestionResult, ScoredRun, Verdict
from sqbyl.project import Project
from sqbyl.projectfiles import load_knowledge, load_semantics
from sqbyl.release import build_release
from sqbyl_runtime.export import (
    McpServer,
    answer_dict,
    as_callable,
    langchain_tool,
    serve_mcp_stdio,
)
from sqbyl_runtime.fingerprint import fingerprint_knowledge, live_schema_fingerprint
from sqbyl_runtime.llm.base import Usage
from sqbyl_runtime.llm.mock import MockLLMClient, structured_reply
from sqbyl_runtime.runtime import Agent, load
from sqbyl_runtime.state.layout import SqbylPaths

_MODEL = "claude-opus-4-8"


def _gen(sql: str) -> object:
    return structured_reply({"plan": "p", "sql": sql, "used_assets": []})


@pytest.fixture
def agent(
    dogfood_dir: Path, duckdb_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Agent:
    """A loaded release over the seeded DuckDB, answering with scripted SQL (mirrors the
    proven test_runtime_load fixture: a synthetic held-out run → build_release → load)."""
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused")
    dst = tmp_path / "proj"
    shutil.copytree(dogfood_dir, dst)
    project = Project.load(dst)
    paths = SqbylPaths(dst).ensure()
    with project.connect() as db:
        schema_fp = live_schema_fingerprint(db, load_semantics(project))
    save_run(
        paths,
        ScoredRun(
            run_id="t",
            split="test",
            models={"agent": _MODEL},
            knowledge_fingerprint=fingerprint_knowledge(load_knowledge(project)),
            schema_fingerprint=schema_fp,
            results=[
                QuestionResult(
                    id="t1",
                    question="q",
                    generated_sql="SELECT 1",
                    verdict=Verdict.correct,
                    usage=Usage(input_tokens=100, output_tokens=20),
                )
            ],
        ),
    )
    release = build_release(project, "v-test")
    llm = MockLLMClient([_gen("SELECT COUNT(*) AS n FROM analytics.orders")] * 12)
    return load(release, db=str(duckdb_path), model=_MODEL, llm=llm)


# --- plain callable --------------------------------------------------------------


def test_as_callable_returns_answer_dict(agent: Agent) -> None:
    call = as_callable(agent)
    out = call("How many orders?")
    assert out["ok"] is True
    assert out["columns"] == ["n"]
    assert out["rows"] == [[2000]]
    assert out["truncated"] is False
    agent.close()


def test_answer_dict_caps_rows() -> None:
    from sqbyl_runtime.pipeline import AgentResult

    result = AgentResult(
        question="q", plan="p", sql="SELECT 1", columns=["x"], rows=[[i] for i in range(500)]
    )
    out = answer_dict(result, row_cap=10)
    assert len(out["rows"]) == 10
    assert out["row_count"] == 500
    assert out["truncated"] is True


def test_answer_dict_row_cap_zero_returns_no_rows() -> None:
    from sqbyl_runtime.pipeline import AgentResult

    # A privacy-conscious MCP launch can return SQL + count only (no rows leave to the client).
    result = AgentResult(question="q", plan="p", sql="SELECT 1", columns=["x"], rows=[[1], [2]])
    out = answer_dict(result, row_cap=0)
    assert out["rows"] == []
    assert out["row_count"] == 2  # the count is still honest
    assert out["truncated"] is True


def test_mcp_tool_description_flags_paid(agent: Agent) -> None:
    listed = McpServer(agent).handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert listed is not None
    assert "paid" in listed["result"]["tools"][0]["description"].lower()
    agent.close()


# --- MCP protocol ----------------------------------------------------------------


def test_mcp_initialize_and_list_tools(agent: Agent) -> None:
    server = McpServer(agent)
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init is not None
    assert init["result"]["protocolVersion"]
    assert init["result"]["capabilities"]["tools"] == {}

    listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert listed is not None
    tools = listed["result"]["tools"]
    assert tools[0]["name"] == "query"
    assert "question" in tools[0]["inputSchema"]["properties"]
    agent.close()


def test_mcp_tool_call_answers(agent: Agent) -> None:
    server = McpServer(agent)
    resp = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "query", "arguments": {"question": "How many orders?"}},
        }
    )
    assert resp is not None
    assert resp["result"]["isError"] is False
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["rows"] == [[2000]]
    agent.close()


def test_mcp_notification_gets_no_response(agent: Agent) -> None:
    server = McpServer(agent)
    # A message with no id is a notification — JSON-RPC says return nothing.
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    agent.close()


def test_mcp_unknown_method_is_an_error(agent: Agent) -> None:
    server = McpServer(agent)
    resp = server.handle({"jsonrpc": "2.0", "id": 9, "method": "nope/nope"})
    assert resp is not None
    assert resp["error"]["code"] == -32601
    agent.close()


def test_mcp_unknown_tool_is_an_error(agent: Agent) -> None:
    server = McpServer(agent)
    resp = server.handle(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "ghost"}}
    )
    assert resp is not None
    assert resp["error"]["code"] == -32601
    agent.close()


def test_mcp_missing_question_is_a_tool_error(agent: Agent) -> None:
    server = McpServer(agent)
    resp = server.handle(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "query"}}
    )
    assert resp is not None
    assert resp["result"]["isError"] is True
    agent.close()


def test_serve_mcp_stdio_round_trip(agent: Agent) -> None:
    # Two framed requests in, two responses out; a blank line and a garbage frame are skipped.
    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        + "\n\nnot json\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})  # no reply
        + "\n"
        + json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "query", "arguments": {"question": "orders?"}},
            }
        )
        + "\n"
    )
    stdout = io.StringIO()
    serve_mcp_stdio(agent, stdin=stdin, stdout=stdout)
    responses = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert [r["id"] for r in responses] == [1, 2]  # notification produced no line
    agent.close()


def test_injected_call_is_used_for_metering(agent: Agent) -> None:
    calls: list[str] = []

    def _metered(question: str) -> dict[str, object]:
        calls.append(question)
        return {"ok": True, "sql": "SELECT 1", "columns": [], "rows": []}

    server = McpServer(agent, call=_metered)
    server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "query", "arguments": {"question": "hi"}},
        }
    )
    assert calls == ["hi"]  # the injected (metered) callable ran, not the raw agent
    agent.close()


# --- CLI: --mcp requires a budget ------------------------------------------------


def test_mcp_cli_requires_budget(capsys: pytest.CaptureFixture[str]) -> None:
    from sqbyl.cli import main

    # No --budget with --mcp is refused up front (autonomous paid consumer needs a hard cap).
    rc = main(["run", "rel.json", "--db", "x", "--model", "m", "--mcp"])
    assert rc == 2
    assert "--mcp requires --budget" in capsys.readouterr().out


# --- LangChain shape (seam) ------------------------------------------------------


def test_langchain_tool_hint_when_missing(agent: Agent) -> None:
    try:
        import langchain_core.tools  # noqa: F401
    except ImportError:
        with pytest.raises(ModuleNotFoundError, match="langchain"):
            langchain_tool(agent)
    else:  # pragma: no cover - only when the optional extra is installed
        tool = langchain_tool(agent)
        assert tool.name == "sql_query"
    agent.close()
