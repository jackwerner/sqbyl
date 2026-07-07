"""Export shapes for a loaded release (spec §10, plan 9.3).

A release loaded by :func:`sqbyl_runtime.load` is an :class:`~sqbyl_runtime.Agent` with
one method, ``ask(question) -> AgentResult``. That's already the whole product; these are
just the **shapes** other ecosystems expect the same agent in — not a foundation, and not
new behavior:

* :func:`as_callable` — a plain ``question -> dict`` function (zero deps).
* :func:`langchain_tool` — a LangChain ``Tool`` wrapping the agent (optional ``langchain``
  extra; lazily imported so the base runtime never pulls it).
* :class:`McpServer` / :func:`serve_mcp_stdio` — an MCP server exposing the agent as a
  ``query`` tool over stdio JSON-RPC. Implemented on the **stdlib** (no ``mcp`` dependency)
  so it stays testable in CI and true to the dependency-light runtime.

All of them route through the same read-only pipeline the agent already runs — an export
shape never gets its own SQL path, so read-only-by-default (invariant 6) travels with them.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import IO, Any

from sqbyl_runtime.pipeline import AgentResult
from sqbyl_runtime.runtime import Agent

# Rows are capped in an export answer so a wrapping LLM/tool isn't handed an unbounded
# result set; the full rows are still available to a direct ``agent.ask`` caller.
_DEFAULT_ROW_CAP = 100


def answer_dict(result: AgentResult, *, row_cap: int = _DEFAULT_ROW_CAP) -> dict[str, Any]:
    """A JSON-able answer: the SQL, the (capped) rows, citations, and provenance.

    Deliberately flat and serialization-safe so it drops straight into a tool response
    or an HTTP body. Rows are the answer the caller asked for; capping only bounds size.
    """
    return {
        "ok": result.ok,
        "sql": result.sql,
        "columns": result.columns,
        "rows": [[_jsonable(v) for v in row] for row in result.rows[:row_cap]],
        "row_count": len(result.rows),
        "truncated": len(result.rows) > row_cap,
        # The opt-in plain-English restatement, present only when narration was enabled; the
        # rows above remain the authoritative answer, this is a convenience over them.
        "answer": result.answer,
        "used_assets": result.used_assets,
        "error": result.error,
        "trace_id": result.trace_id,
    }


def as_callable(
    agent: Agent, *, row_cap: int = _DEFAULT_ROW_CAP
) -> Callable[[str], dict[str, Any]]:
    """The agent as a plain ``question -> answer_dict`` function — the smallest export."""

    def _call(question: str) -> dict[str, Any]:
        return answer_dict(agent.ask(question), row_cap=row_cap)

    return _call


_DEFAULT_TOOL_NAME = "sql_query"
_DEFAULT_TOOL_DESCRIPTION = (
    "Answer a natural-language question about the database by generating and running a "
    "read-only SQL query. Input: the question in plain English. Returns the SQL, the "
    "result rows, and any trusted assets it used."
)


def langchain_tool(
    agent: Agent,
    *,
    name: str = _DEFAULT_TOOL_NAME,
    description: str = _DEFAULT_TOOL_DESCRIPTION,
    row_cap: int = _DEFAULT_ROW_CAP,
) -> Any:
    """Wrap the agent as a LangChain ``Tool`` (question -> JSON answer string).

    LangChain is an optional extra; it's imported lazily so the base runtime never depends
    on it. Missing → a clear ``pip install 'sqbyl-runtime[langchain]'`` hint.
    """
    try:
        from langchain_core.tools import Tool
    except ImportError as exc:
        raise ModuleNotFoundError(
            "langchain isn't installed; run `pip install 'sqbyl-runtime[langchain]'` "
            "to export a release as a LangChain tool"
        ) from exc

    call = as_callable(agent, row_cap=row_cap)

    def _run(question: str) -> str:
        return json.dumps(call(question))

    return Tool(name=name, description=description, func=_run)


# --- MCP over stdio (stdlib JSON-RPC, no dependency) -----------------------------

_MCP_PROTOCOL_VERSION = "2024-11-05"
_JSONRPC_METHOD_NOT_FOUND = -32601


class McpServer:
    """Expose a loaded agent as an MCP server with a single ``query`` tool.

    Speaks the subset of MCP a text-to-SQL tool needs — ``initialize``, ``tools/list``,
    ``tools/call`` — as JSON-RPC 2.0. :meth:`handle` is a pure ``request -> response`` so
    the protocol is unit-testable without any transport; :func:`serve_mcp_stdio` wires it
    to stdin/stdout for a client (e.g. Claude Desktop) to launch as a subprocess.
    """

    def __init__(
        self,
        agent: Agent,
        *,
        tool_name: str = "query",
        row_cap: int = _DEFAULT_ROW_CAP,
        call: Callable[[str], dict[str, Any]] | None = None,
    ):
        self._agent = agent
        self._tool_name = tool_name
        # A caller can inject a metered/budgeted callable (e.g. the CLI, so MCP tool calls
        # meter to usage.db and honor --budget); otherwise the plain agent callable is used.
        self._call = call or as_callable(agent, row_cap=row_cap)

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """Handle one JSON-RPC request; return the response, or ``None`` for a notification.

        A notification (a request with no ``id``, e.g. ``notifications/initialized``) gets
        no reply, per JSON-RPC — the transport simply reads the next message.
        """
        method = request.get("method")
        req_id = request.get("id")
        if req_id is None:
            return None  # notification: no response
        if method == "initialize":
            return _ok(req_id, self._initialize_result())
        if method == "tools/list":
            return _ok(req_id, {"tools": [self._tool_schema()]})
        if method == "tools/call":
            return self._call_tool(req_id, request.get("params") or {})
        return _err(req_id, _JSONRPC_METHOD_NOT_FOUND, f"method not found: {method}")

    def _initialize_result(self) -> dict[str, Any]:
        return {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "sqbyl", "version": "0.0.0"},
        }

    def _tool_schema(self) -> dict[str, Any]:
        return {
            "name": self._tool_name,
            # Tell the consuming agent this tool costs money, so a well-behaved caller can
            # weigh calls and relay cost (responsible-ai: in-band spend legibility).
            "description": _DEFAULT_TOOL_DESCRIPTION + " Each call is a paid LLM invocation.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question in plain English."}
                },
                "required": ["question"],
            },
        }

    def _call_tool(self, req_id: object, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("name") != self._tool_name:
            return _err(req_id, _JSONRPC_METHOD_NOT_FOUND, f"unknown tool: {params.get('name')}")
        question = str((params.get("arguments") or {}).get("question", "")).strip()
        if not question:
            return _tool_result(req_id, "error: missing 'question' argument", is_error=True)
        answer = self._call(question)
        return _tool_result(req_id, json.dumps(answer), is_error=not answer["ok"])


def serve_mcp_stdio(
    agent: Agent,
    *,
    call: Callable[[str], dict[str, Any]] | None = None,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
) -> None:
    """Run the MCP server over stdio until stdin closes — the transport an MCP client drives.

    One JSON-RPC message per line (newline-delimited JSON); responses flushed as written.
    ``call`` injects a metered/budgeted callable (the CLI passes one so tool calls meter to
    usage.db and honor ``--budget``).
    """
    server = McpServer(agent, call=call)
    inp = stdin or sys.stdin
    out = stdout or sys.stdout
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue  # skip a malformed frame rather than crash the server
        response = server.handle(request)
        if response is not None:
            out.write(json.dumps(response) + "\n")
            out.flush()


# --- helpers ---------------------------------------------------------------------


def _ok(req_id: object, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: object, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _tool_result(req_id: object, text: str, *, is_error: bool) -> dict[str, Any]:
    return _ok(
        req_id,
        {"content": [{"type": "text", "text": text}], "isError": is_error},
    )


def _jsonable(value: object) -> object:
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)
