"""sqbyl-runtime — the minimal, shippable sqbyl runtime.

Contains only what a production app needs to embed a released agent: release
``load()``, ``ask()``, the ``LLMClient`` seam, and structured logging. None of
the dev toolkit (eval, synth, Coach, judges, console) lives here or is importable
from here — that one-way dependency arrow is enforced by import-linter in CI.

    from sqbyl_runtime import load
    agent = load("revenue-analytics.v3.json", db=DATABASE_URL, model="claude-opus-4-8")
    agent.ask("How many orders shipped last month?")   # → AgentResult
"""

from sqbyl_runtime.export import (
    McpServer,
    answer_dict,
    as_callable,
    langchain_tool,
    serve_mcp_stdio,
)
from sqbyl_runtime.runtime import (
    Agent,
    ModelMismatchWarning,
    SchemaMismatchWarning,
    load,
)

__version__ = "0.0.0"

__all__ = [
    "Agent",
    "McpServer",
    "ModelMismatchWarning",
    "SchemaMismatchWarning",
    "answer_dict",
    "as_callable",
    "langchain_tool",
    "load",
    "serve_mcp_stdio",
    "__version__",
]
