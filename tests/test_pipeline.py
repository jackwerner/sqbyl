"""Phase 2.2 — the agent pipeline ``ask()`` (spec §5 steps 3-7).

Exit criteria: against recorded/scripted model responses, ``ask()`` answers the
dogfood questions end-to-end; self-repair is exercised by a fixture that returns
bad-then-good SQL; every run writes an OTel-shaped trace. All zero-token (mock seam).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sqbyl.project import Project
from sqbyl.projectfiles import load_knowledge
from sqbyl_runtime.context import ProjectKnowledge
from sqbyl_runtime.db import Database
from sqbyl_runtime.llm.mock import MockLLMClient, structured_reply
from sqbyl_runtime.models import Dialect
from sqbyl_runtime.pipeline import ask
from sqbyl_runtime.state.traces import TraceWriter, read_spans


@pytest.fixture
def knowledge(dogfood_dir: Path) -> ProjectKnowledge:
    return load_knowledge(Project.load(dogfood_dir))


@pytest.fixture
def db(duckdb_path: Path) -> Database:
    return Database.connect(str(duckdb_path), dialect=Dialect.duckdb)


def _gen(sql: str, *, plan: str = "p", used_assets: list[str] | None = None) -> object:
    return structured_reply({"plan": plan, "sql": sql, "used_assets": used_assets or []})


def test_answers_a_question_end_to_end(knowledge: ProjectKnowledge, db: Database) -> None:
    llm = MockLLMClient([_gen("SELECT COUNT(*) AS n FROM analytics.orders")])
    result = ask(
        "How many orders are there in total?",
        knowledge=knowledge,
        db=db,
        llm=llm,
        model="claude-opus-4-8",
    )
    assert result.ok
    assert result.attempts == 1 and result.repaired is False
    assert result.columns == ["n"]
    assert result.rows == [[2000]]
    # Tables are loaded in sorted filename order (customers.yaml, orders.yaml).
    assert result.selected_tables == ["analytics.customers", "analytics.orders"]
    assert llm.call_count == 1
    db.close()


def test_self_repair_recovers_from_bad_sql(knowledge: ProjectKnowledge, db: Database) -> None:
    llm = MockLLMClient(
        [
            _gen("SELECT bogus_col FROM analytics.orders"),  # fails static validation
            _gen(
                "SELECT SUM(amount_cents)/100.0 AS net FROM analytics.orders "
                "WHERE status='confirmed'",
                used_assets=["monthly_recurring_revenue"],
            ),
        ]
    )
    result = ask("total net revenue?", knowledge=knowledge, db=db, llm=llm, model="m")
    assert result.ok
    assert result.attempts == 2 and result.repaired is True
    assert llm.call_count == 2
    # The repair turn fed the binder error back to the model.
    assert "bogus_col" in llm.requests[1].messages[-1].content
    # Only offered assets the model claims get cited.
    assert result.used_assets == ["monthly_recurring_revenue"]
    db.close()


def test_citation_drops_unoffered_assets(knowledge: ProjectKnowledge, db: Database) -> None:
    llm = MockLLMClient(
        [_gen("SELECT COUNT(*) AS n FROM analytics.orders", used_assets=["made_up_asset"])]
    )
    result = ask("count", knowledge=knowledge, db=db, llm=llm, model="m")
    assert result.used_assets == []  # a hallucinated asset is not cited
    db.close()


def test_failure_after_exhausting_repairs(knowledge: ProjectKnowledge, db: Database) -> None:
    llm = MockLLMClient([_gen("SELECT nope FROM analytics.orders")] * 3)
    result = ask("bad", knowledge=knowledge, db=db, llm=llm, model="m", self_repair_attempts=2)
    assert not result.ok
    assert result.attempts == 3  # 1 initial + 2 repairs
    assert llm.call_count == 3
    assert result.error and "nope" in result.error
    assert result.rows == []
    db.close()


def test_unparseable_sql_fails_gracefully(knowledge: ProjectKnowledge, db: Database) -> None:
    # A generation sqlglot cannot parse (unquoted spaced identifiers, as in real BIRD
    # schemas) must become a failed answer that feeds self-repair — not an exception
    # that propagates out of ask() and aborts a whole eval run.
    bad = "SELECT COUNT(DISTINCT School Name) FROM analytics.orders WHERE County Name = 'x'"
    llm = MockLLMClient([_gen(bad)] * 3)
    result = ask(
        "count schools", knowledge=knowledge, db=db, llm=llm, model="m", self_repair_attempts=2
    )
    assert not result.ok
    assert result.attempts == 3  # it retried rather than crashing on the first bad gen
    assert result.error is not None
    assert result.rows == []
    db.close()


def test_write_sql_is_refused_not_executed(knowledge: ProjectKnowledge, db: Database) -> None:
    # Even if the model emits a write, the read-only guard refuses it (it never runs).
    llm = MockLLMClient([_gen("DELETE FROM analytics.orders")] * 3)
    result = ask("drop everything", knowledge=knowledge, db=db, llm=llm, model="m")
    assert not result.ok
    assert result.error is not None
    # The table is untouched.
    assert db.execute("SELECT COUNT(*) FROM analytics.orders").rows[0][0] == 2000
    db.close()


def test_selection_fallback_surfaces_on_result_and_trace(
    knowledge: ProjectKnowledge, db: Database, tmp_path: Path
) -> None:
    # Force an llm strategy whose shortlist returns nothing → fallback to include-all.
    from sqbyl_runtime.models import SelectionConfig

    narrowed = knowledge.model_copy(update={"selection": SelectionConfig(strategy="llm")})
    writer = TraceWriter(tmp_path / "trace.jsonl")
    llm = MockLLMClient(
        [
            structured_reply({"tables": []}),  # selection: matches nothing
            _gen("SELECT COUNT(*) AS n FROM analytics.orders"),  # generate
        ]
    )
    result = ask("count", knowledge=narrowed, db=db, llm=llm, model="m", trace_writer=writer)
    # The degradation is legible on the result, not just buried in the compiler.
    assert result.selection_fell_back is True
    assert result.selection_strategy == "include_all"
    assert any("matched no tables" in n for n in result.selection_notes)
    # ...and on the run span, so it's auditable in the trace (invariant 7 / transparency).
    run = next(s for s in read_spans(tmp_path / "trace.jsonl") if s.name == "ask")
    assert run.attributes["sqbyl.selection.fell_back"] is True
    assert run.attributes["sqbyl.selection.strategy"] == "include_all"
    db.close()


def test_run_writes_otel_trace(knowledge: ProjectKnowledge, db: Database, tmp_path: Path) -> None:
    writer = TraceWriter(tmp_path / "trace.jsonl")
    llm = MockLLMClient([_gen("SELECT COUNT(*) AS n FROM analytics.orders")])
    result = ask("count", knowledge=knowledge, db=db, llm=llm, model="m", trace_writer=writer)

    spans = read_spans(tmp_path / "trace.jsonl")
    run = next(s for s in spans if s.name == "ask")
    llm_spans = [s for s in spans if s.name != "ask"]
    assert run.status == "ok"
    assert run.attributes["gen_ai.operation.name"] == "chat"
    assert all(s.trace_id == result.trace_id for s in spans)
    assert all(s.parent_span_id == run.span_id for s in llm_spans)
    assert llm_spans[0].attributes["gen_ai.usage.input_tokens"] >= 0
    db.close()
