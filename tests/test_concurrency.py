"""The shipped runtime under concurrent load (sqbyl-enhancements.md §2.3).

A typical enterprise API serves ``ask()`` from a threadpool (e.g. FastAPI running a
sync endpoint, or ``run_in_threadpool``), so one loaded ``Agent`` must be safe to call
from many threads at once. These tests exercise that: an end-to-end concurrent run, plus
targeted proofs of the two shared-mutable pieces that were made thread-safe — the trace
writer's appends and the lazy SDK-client construction.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from sqbyl.project import Project
from sqbyl.projectfiles import load_knowledge
from sqbyl_runtime.context import ProjectKnowledge
from sqbyl_runtime.db import Database
from sqbyl_runtime.llm.anthropic_client import AnthropicLLMClient
from sqbyl_runtime.llm.base import LLMClient, LLMRequest, LLMResponse, Usage
from sqbyl_runtime.models import Dialect
from sqbyl_runtime.pipeline import ask
from sqbyl_runtime.state.traces import Span, TraceWriter


class _ConstantClient(LLMClient):
    """Stateless, hence thread-safe: the same valid structured reply for every call."""

    def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            model=request.model,
            structured={
                "plan": "count the orders",
                "sql": "SELECT COUNT(*) AS n FROM analytics.orders",
                "used_assets": [],
            },
            usage=Usage(input_tokens=1, output_tokens=1),
        )


@pytest.fixture
def knowledge(dogfood_dir: Path) -> ProjectKnowledge:
    return load_knowledge(Project.load(dogfood_dir))


def test_concurrent_ask_is_safe_end_to_end(
    knowledge: ProjectKnowledge, duckdb_path: Path, tmp_path: Path
) -> None:
    # One shared DB + one shared trace writer + one shared client, hit from 8 threads.
    db = Database.connect(str(duckdb_path), dialect=Dialect.duckdb)
    writer = TraceWriter(tmp_path / "traces.jsonl")
    llm = _ConstantClient()

    def one(_i: int) -> object:
        return ask(
            "How many orders are there?",
            knowledge=knowledge,
            db=db,
            llm=llm,
            model="claude-opus-4-8",
            trace_writer=writer,
        )

    n = 24
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, range(n)))
    db.close()

    # Every concurrent call returned the correct answer — no cross-talk between runs.
    assert len(results) == n
    assert all(r.ok and r.rows == [[2000]] for r in results)

    # Every trace line is intact JSON: the writer lock kept concurrent appends from
    # interleaving into corrupt lines.
    lines = (tmp_path / "traces.jsonl").read_text().splitlines()
    assert lines
    for line in lines:
        json.loads(line)  # raises on a torn/interleaved line


def test_trace_writer_appends_are_thread_safe(tmp_path: Path) -> None:
    writer = TraceWriter(tmp_path / "t.jsonl")
    threads, per_thread = 8, 50

    def spam(tid: int) -> None:
        for i in range(per_thread):
            writer.write(Span(name=f"t{tid}-{i}", trace_id="x" * 32))

    with ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(spam, range(threads)))

    lines = (tmp_path / "t.jsonl").read_text().splitlines()
    assert len(lines) == threads * per_thread  # nothing dropped
    for line in lines:
        json.loads(line)  # nothing torn


def test_lazy_client_construction_is_race_safe() -> None:
    # Concurrent first calls must share a single SDK client, not build one each.
    client = AnthropicLLMClient(api_key="sk-test")
    seen: list[object] = []
    barrier = threading.Barrier(8)

    def build(_i: int) -> None:
        barrier.wait()  # maximize the chance of a simultaneous first call
        seen.append(client._ensure_client())

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(build, range(8)))

    assert len(seen) == 8
    assert all(c is seen[0] for c in seen)  # one shared instance
