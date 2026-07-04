"""The selection eval (spec §5.1, plan 9.1) — scoring the shortlister on gold tables.

Expected tables are derived from each question's ``gold_sql``; recall is the fraction
of questions where selection kept *every* gold table. All deterministic (lexical), so
no tokens.
"""

from __future__ import annotations

from sqbyl.eval.selection import evaluate_selection
from sqbyl.models.benchmarks import BenchmarkQuestion
from sqbyl_runtime.context import ProjectKnowledge
from sqbyl_runtime.models import Column, Dialect, SelectionConfig, TableSemantics


def _schema() -> list[TableSemantics]:
    return [
        TableSemantics(table="orders", columns=[Column(name="amount_cents", type="int")]),
        TableSemantics(table="customers", columns=[Column(name="region", type="text")]),
        TableSemantics(table="inventory", columns=[Column(name="on_hand", type="int")]),
    ]


def _knowledge(cfg: SelectionConfig) -> ProjectKnowledge:
    return ProjectKnowledge(dialect=Dialect.duckdb, semantics=_schema(), selection=cfg)


def test_recall_is_perfect_when_selection_keeps_gold_tables() -> None:
    questions = [
        BenchmarkQuestion(
            id="q1",
            question="total orders revenue",
            gold_sql="SELECT sum(amount_cents) FROM orders",
        ),
    ]
    cfg = SelectionConfig(strategy="lexical", max_tables=2)
    report = evaluate_selection(questions, semantics=_schema(), knowledge=_knowledge(cfg))
    assert report.recall == 1.0
    assert report.scored == 1
    assert report.items[0].covered
    assert report.items[0].expected == ["orders"]


def test_missed_gold_table_drops_recall() -> None:
    # The question matches a *different* table (customers/region), so lexical narrows to
    # that and no fallback fires — yet the gold answer needs inventory → a genuine miss.
    questions = [
        BenchmarkQuestion(
            id="q1",
            question="customers by region",
            gold_sql="SELECT on_hand FROM inventory",
        ),
    ]
    cfg = SelectionConfig(strategy="lexical", max_tables=1)
    report = evaluate_selection(questions, semantics=_schema(), knowledge=_knowledge(cfg))
    assert report.recall == 0.0
    item = report.items[0]
    assert item.expected == ["inventory"]
    assert item.missed == ["inventory"]
    assert not item.covered


def test_questions_without_gold_sql_are_skipped() -> None:
    questions = [
        BenchmarkQuestion(id="q1", question="via asset", gold_asset="mrr"),
        BenchmarkQuestion(id="q2", question="orders", gold_sql="SELECT 1 FROM orders"),
    ]
    cfg = SelectionConfig(strategy="include_all")
    report = evaluate_selection(questions, semantics=_schema(), knowledge=_knowledge(cfg))
    assert report.skipped == 1
    assert report.scored == 1


def test_extra_tables_are_reported_but_do_not_hurt_recall() -> None:
    questions = [
        BenchmarkQuestion(id="q1", question="orders", gold_sql="SELECT 1 FROM orders"),
    ]
    cfg = SelectionConfig(strategy="include_all")  # keeps all 3 tables
    report = evaluate_selection(questions, semantics=_schema(), knowledge=_knowledge(cfg))
    assert report.recall == 1.0  # gold table 'orders' is present
    assert set(report.items[0].extra) == {"customers", "inventory"}


def test_report_carries_wilson_bounds_around_recall() -> None:
    # A perfect recall on one question is not certain — the Wilson lower bound is well under 1.
    questions = [
        BenchmarkQuestion(id="q1", question="orders", gold_sql="SELECT 1 FROM orders"),
    ]
    cfg = SelectionConfig(strategy="include_all")
    report = evaluate_selection(questions, semantics=_schema(), knowledge=_knowledge(cfg))
    low, high = report.recall_interval
    assert report.recall == 1.0
    assert low < 1.0  # a single success is not proof; the interval says so
    assert high <= 1.0


def test_deterministic_strategy_has_no_model_stamp() -> None:
    questions = [BenchmarkQuestion(id="q1", question="orders", gold_sql="SELECT 1 FROM orders")]
    cfg = SelectionConfig(strategy="lexical", max_tables=1)
    report = evaluate_selection(questions, semantics=_schema(), knowledge=_knowledge(cfg))
    assert report.model is None  # no tokens spent → nothing to stamp


def test_gold_table_named_only_in_a_comment_is_not_counted() -> None:
    # A declared table mentioned only in a SQL comment must not enter the expected set.
    questions = [
        BenchmarkQuestion(
            id="q1",
            question="orders",
            gold_sql="SELECT 1 FROM orders -- not from inventory",
        ),
    ]
    cfg = SelectionConfig(strategy="include_all")
    report = evaluate_selection(questions, semantics=_schema(), knowledge=_knowledge(cfg))
    assert report.items[0].expected == ["orders"]  # inventory (comment-only) excluded
