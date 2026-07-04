"""Phase 9.2 — `sqbyl serve` / `run <release>` local chat endpoint (spec §9.2).

The HTTP server is exercised end-to-end over a real socket (ephemeral port) with a
scripted MockLLMClient — no tokens (invariant 4). Covers /ask (rows returned),
/feedback (persisted as an eval candidate, no row data), the session budget hard-stop,
and the not-localhost safety warning.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from sqbyl.project import Project
from sqbyl.serve import ChatServer, is_local_host, make_server, project_endpoint
from sqbyl_runtime.llm.mock import MockLLMClient, structured_reply
from sqbyl_runtime.state.layout import SqbylPaths


def _gen(sql: str) -> object:
    return structured_reply({"plan": "count orders", "sql": sql, "used_assets": []})


@pytest.fixture
def chat(
    dogfood_dir: Path, duckdb_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> ChatServer:
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))
    project = Project.load(dogfood_dir)
    # A generous reply queue so several /ask calls in one test each get scripted SQL.
    llm = MockLLMClient([_gen("SELECT COUNT(*) AS n FROM analytics.orders")] * 8)
    endpoint = project_endpoint(project, llm=llm)
    # Keep .sqbyl writes (usage.db, feedback.jsonl) out of the repo — point the paths at tmp.
    return ChatServer(endpoint, paths=SqbylPaths(tmp_path))


def _serve_in_thread(chat: ChatServer) -> tuple[object, str]:
    server = make_server(chat, host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


def _post(base: str, path: str, body: dict[str, object]) -> dict[str, object]:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - localhost test server
        return json.loads(resp.read())


def test_ask_returns_rows_over_http(chat: ChatServer) -> None:
    server, base = _serve_in_thread(chat)
    try:
        data = _post(base, "/ask", {"question": "How many orders?"})
        assert data["ok"] is True
        assert data["columns"] == ["n"]
        assert data["rows"] == [[2000]]
        assert data["cost_usd"] >= 0
        assert data["trace_id"]
    finally:
        server.shutdown()
        server.server_close()
        chat.close()


def test_index_page_served(chat: ChatServer) -> None:
    server, base = _serve_in_thread(chat)
    try:
        with urllib.request.urlopen(base + "/", timeout=5) as resp:  # noqa: S310
            html = resp.read().decode()
        assert "<title>sqbyl</title>" in html
        assert "/ask" in html  # the page posts to the ask endpoint
    finally:
        server.shutdown()
        server.server_close()
        chat.close()


def test_feedback_is_persisted_without_row_data(chat: ChatServer) -> None:
    server, base = _serve_in_thread(chat)
    try:
        ask = _post(base, "/ask", {"question": "How many orders?"})
        out = _post(
            base,
            "/feedback",
            {
                "trace_id": ask["trace_id"],
                "question": "How many orders?",
                "sql": ask["sql"],
                "rating": "up",
                "ok": True,
            },
        )
        assert out["stored"] is True
    finally:
        server.shutdown()
        server.server_close()
        chat.close()

    lines = chat.paths.feedback_log.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["rating"] == "up"
    assert record["sql"].startswith("SELECT")
    # No result rows persisted (spec §13) — only question + SQL + rating.
    assert "rows" not in record and "columns" not in record


def test_session_budget_hard_stops(
    dogfood_dir: Path, duckdb_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))
    project = Project.load(dogfood_dir)
    llm = MockLLMClient([_gen("SELECT COUNT(*) AS n FROM analytics.orders")] * 8)
    # A budget so tiny the first question's per-call estimate already exceeds it.
    chat = ChatServer(project_endpoint(project, llm=llm), paths=SqbylPaths(tmp_path), budget=1e-9)
    server, base = _serve_in_thread(chat)
    try:
        data = _post(base, "/ask", {"question": "How many orders?"})
        assert data["budget_exhausted"] is True
        assert "budget" in data["error"]
    finally:
        server.shutdown()
        server.server_close()
        chat.close()
    # Nothing was spent — the call never ran.
    assert chat.spent == 0.0


def test_meta_exposes_pre_spend_consent(
    dogfood_dir: Path, duckdb_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The page fetches /meta on load so the per-call estimate + budget are visible BEFORE
    # the first click (responsible-ai: browser-side spend consent).
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))
    project = Project.load(dogfood_dir)
    llm = MockLLMClient([_gen("SELECT 1")])
    chat = ChatServer(project_endpoint(project, llm=llm), paths=SqbylPaths(tmp_path), budget=2.0)
    server, base = _serve_in_thread(chat)
    try:
        with urllib.request.urlopen(base + "/meta", timeout=5) as resp:  # noqa: S310
            meta = json.loads(resp.read())
        assert meta["per_call_estimate_usd"] > 0  # a real dollar figure, shown pre-spend
        assert meta["budget"] == 2.0
        assert meta["spent_usd"] == 0.0
    finally:
        server.shutdown()
        server.server_close()
        chat.close()


def test_is_local_host() -> None:
    assert is_local_host("127.0.0.1")
    assert is_local_host("localhost")
    assert not is_local_host("0.0.0.0")  # noqa: S104 - asserting we treat bind-all as non-local
    assert not is_local_host("192.168.1.5")


def test_missing_question_is_rejected(chat: ChatServer) -> None:
    server, base = _serve_in_thread(chat)
    try:
        req = urllib.request.Request(
            base + "/ask",
            data=json.dumps({"question": "   "}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:  # noqa: S310
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        chat.close()
