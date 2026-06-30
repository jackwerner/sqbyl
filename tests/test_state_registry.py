"""Phase 0.5 — usage rows + OTel-shaped traces write/read; content-hash is stable.

Exit criteria: a usage row and a trace row round-trip, and the dogfood project's
content hash is identical across repeated runs (so runs link to an exact config).
"""

from __future__ import annotations

from pathlib import Path

from sqbyl.state import content_hash, tracked_files
from sqbyl_runtime.llm.base import LLMRequest, LLMResponse, Message, Usage
from sqbyl_runtime.state import (
    SqbylPaths,
    TraceWriter,
    UsageRecord,
    UsageStore,
    llm_call_span,
    read_spans,
)
from sqbyl_runtime.state.traces import (
    GEN_AI_REQUEST_MODEL,
    GEN_AI_SYSTEM,
    GEN_AI_USAGE_INPUT_TOKENS,
)


def test_layout_creates_skeleton(tmp_path: Path) -> None:
    paths = SqbylPaths(tmp_path).ensure()
    assert paths.root.is_dir()
    assert paths.traces_dir.is_dir()
    assert paths.runs_dir.is_dir()
    assert paths.usage_db == tmp_path / ".sqbyl" / "usage.db"


def test_usage_row_writes_and_reads_back(tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "usage.db")
    usage = Usage(input_tokens=120, output_tokens=30, cache_read_input_tokens=400)
    rec = UsageRecord.from_usage(
        usage, model="claude-opus-4-8", command="ask", role="agent", cost_usd=0.012, run_id="r1"
    )
    rid = store.record(rec)
    assert rid == 1

    rows = store.all()
    assert len(rows) == 1
    got = rows[0]
    assert got.model == "claude-opus-4-8"
    assert got.command == "ask"
    assert got.input_tokens == 120
    assert got.cache_read_input_tokens == 400
    assert store.total_cost() == 0.012
    assert store.total_cost(run_id="r1") == 0.012
    assert store.total_cost(run_id="other") == 0.0
    assert store.total_tokens() == 550
    store.close()


def test_usage_store_persists_across_connections(tmp_path: Path) -> None:
    db = tmp_path / "usage.db"
    with UsageStore(db) as store:
        store.record(UsageRecord(model="m", input_tokens=5))
    # Reopen — the row is durable.
    with UsageStore(db) as store2:
        assert len(store2.all()) == 1


def test_trace_span_is_otel_shaped_and_roundtrips(tmp_path: Path) -> None:
    request = LLMRequest(
        model="claude-opus-4-8",
        messages=[Message(role="user", content="net revenue?")],
        temperature=0.0,
    )
    response = LLMResponse(
        model="claude-opus-4-8",
        text="SELECT 1",
        stop_reason="end_turn",
        usage=Usage(input_tokens=42, output_tokens=7),
    )
    span = llm_call_span(request, response)
    # OTel GenAI attribute names are present from the first trace written.
    assert span.attributes[GEN_AI_SYSTEM] == "anthropic"
    assert span.attributes[GEN_AI_REQUEST_MODEL] == "claude-opus-4-8"
    assert span.attributes[GEN_AI_USAGE_INPUT_TOKENS] == 42
    assert span.status == "ok"
    assert span.end_time is not None
    assert len(span.trace_id) == 32 and len(span.span_id) == 16

    writer = TraceWriter(tmp_path / "traces" / "run.jsonl")
    writer.write(span)
    writer.write(llm_call_span(request, response))  # a second span in the same file

    spans = read_spans(tmp_path / "traces" / "run.jsonl")
    assert len(spans) == 2
    assert spans[0] == span


def test_read_spans_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_spans(tmp_path / "nope.jsonl") == []


def test_content_hash_is_stable_across_runs(dogfood_dir: Path) -> None:
    h1 = content_hash(dogfood_dir)
    h2 = content_hash(dogfood_dir)
    assert h1 == h2
    assert h1.startswith("sha256:")
    # It actually covers the project files (manifest, both semantics, benchmarks…).
    files = {p.name for p in tracked_files(dogfood_dir)}
    assert {"sqbyl.yaml", "orders.yaml", "customers.yaml", "dev.yaml", "test.yaml"} <= files


def test_content_hash_changes_when_content_changes(tmp_path: Path) -> None:
    (tmp_path / "sqbyl.yaml").write_text("name: x\n")
    before = content_hash(tmp_path)
    (tmp_path / "sqbyl.yaml").write_text("name: y\n")
    after = content_hash(tmp_path)
    assert before != after


def test_content_hash_ignores_sqbyl_state(tmp_path: Path) -> None:
    (tmp_path / "sqbyl.yaml").write_text("name: x\n")
    before = content_hash(tmp_path)
    # Writing local state must not perturb the config hash.
    SqbylPaths(tmp_path).ensure()
    UsageStore(SqbylPaths(tmp_path).usage_db).record(UsageRecord(model="m"))
    assert content_hash(tmp_path) == before
