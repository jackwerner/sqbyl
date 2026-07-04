"""Large-schema context selection (spec §5.1, plan 9.1).

Covers the four strategies (include_all / lexical / llm / llm_lexical), value-matching,
the compiler wiring (narrowed tables + relevant examples + value hints, and selection
usage threaded onto the compiled context), and the graceful fallbacks. Everything runs
on the ``MockLLMClient`` — no network, no tokens (invariant 4).
"""

from __future__ import annotations

from sqbyl_runtime.context import compile_context
from sqbyl_runtime.llm.base import Usage
from sqbyl_runtime.llm.mock import MockLLMClient, structured_reply
from sqbyl_runtime.models import Column, Dialect, Example, SelectionConfig, TableSemantics
from sqbyl_runtime.selection import select_context

# --- a small "wide" schema to select over ---------------------------------------


def _schema() -> list[TableSemantics]:
    return [
        TableSemantics(
            table="orders",
            description="customer orders, one row per order",
            columns=[
                Column(name="order_id", type="int"),
                Column(name="status", type="text", sample_values=["confirmed", "pending"]),
                Column(name="amount_cents", type="int"),
            ],
        ),
        TableSemantics(
            table="customers",
            description="people who place orders",
            synonyms=["clients", "accounts"],
            columns=[
                Column(name="customer_id", type="int"),
                Column(name="region", type="text", sample_values=["emea", "amer", "apac"]),
            ],
        ),
        TableSemantics(
            table="shipments",
            description="delivery records for fulfilled orders",
            columns=[Column(name="shipment_id", type="int"), Column(name="carrier", type="text")],
        ),
        TableSemantics(
            table="inventory",
            description="warehouse stock levels",
            columns=[Column(name="sku", type="text"), Column(name="on_hand", type="int")],
        ),
    ]


# --- include_all (the default) ---------------------------------------------------


def test_include_all_keeps_every_table_and_spends_nothing() -> None:
    sel = select_context("anything at all", semantics=_schema(), config=SelectionConfig())
    assert sel.tables == ["orders", "customers", "shipments", "inventory"]
    assert sel.usage.total_tokens == 0


# --- lexical (deterministic, $0) -------------------------------------------------


def test_lexical_narrows_to_question_relevant_tables() -> None:
    cfg = SelectionConfig(strategy="lexical", max_tables=2)
    sel = select_context("how many customers placed orders", semantics=_schema(), config=cfg)
    # "customers" and "orders" appear in the question; inventory/shipments do not.
    assert set(sel.tables) == {"orders", "customers"}
    assert sel.usage.total_tokens == 0


def test_lexical_matches_on_synonyms() -> None:
    cfg = SelectionConfig(strategy="lexical", max_tables=1)
    sel = select_context("count of clients", semantics=_schema(), config=cfg)
    assert sel.tables == ["customers"]  # matched via the "clients" synonym


def test_lexical_output_is_in_schema_order() -> None:
    cfg = SelectionConfig(strategy="lexical", max_tables=4)
    # Question mentions shipments before customers, but output follows schema order.
    sel = select_context("shipments for customers", semantics=_schema(), config=cfg)
    assert sel.tables == ["orders", "customers", "shipments"] or sel.tables == [
        "customers",
        "shipments",
    ]
    # Whatever survives, it is a subsequence of the declared order.
    order = ["orders", "customers", "shipments", "inventory"]
    assert sel.tables == [t for t in order if t in set(sel.tables)]


def test_lexical_with_no_match_falls_back_to_all() -> None:
    cfg = SelectionConfig(strategy="lexical", max_tables=2)
    sel = select_context("zzzz qqqq nonsense", semantics=_schema(), config=cfg)
    assert len(sel.tables) == 4  # can't answer with an empty schema → include all
    assert any("no tables" in n for n in sel.notes)


# --- value-matching --------------------------------------------------------------


def test_value_matching_maps_a_literal_to_a_declared_value() -> None:
    cfg = SelectionConfig(strategy="include_all", value_matching=True)
    sel = select_context("revenue in EMEA region", semantics=_schema(), config=cfg)
    matches = {(m.column, m.value) for m in sel.value_matches}
    assert ("region", "emea") in matches


def test_value_matching_off_by_default() -> None:
    sel = select_context("revenue in EMEA", semantics=_schema(), config=SelectionConfig())
    assert sel.value_matches == []


def test_value_matching_skips_suppressed_pii_columns() -> None:
    # A column whose sample_values were suppressed (PII) never yields a value hint.
    schema = [
        TableSemantics(
            table="users",
            columns=[Column(name="email", type="text", sample_values=None)],
        )
    ]
    cfg = SelectionConfig(strategy="include_all", value_matching=True)
    sel = select_context("show alice@example.com", semantics=schema, config=cfg)
    assert sel.value_matches == []


def test_value_matching_enforces_profile_false_optout_on_read_path() -> None:
    # Even if a stale/hand-authored sample_values block survives on a column the human
    # opted out of profiling (profile: false), the read path refuses to surface it.
    schema = [
        TableSemantics(
            table="users",
            columns=[Column(name="ssn", type="text", sample_values=["secret"], profile=False)],
        )
    ]
    cfg = SelectionConfig(strategy="include_all", value_matching=True)
    sel = select_context("look up secret", semantics=schema, config=cfg)
    assert sel.value_matches == []


# --- llm shortlisting ------------------------------------------------------------


def test_llm_shortlist_selects_named_tables_and_meters_usage() -> None:
    llm = MockLLMClient(
        [structured_reply({"tables": ["orders", "customers"]}, usage=Usage(input_tokens=40))]
    )
    cfg = SelectionConfig(strategy="llm")
    sel = select_context("revenue by customer", semantics=_schema(), config=cfg, llm=llm, model="m")
    assert set(sel.tables) == {"orders", "customers"}
    assert sel.usage.input_tokens == 40
    assert llm.call_count == 1


def test_llm_invented_table_is_dropped() -> None:
    llm = MockLLMClient([structured_reply({"tables": ["orders", "made_up_table"]})])
    cfg = SelectionConfig(strategy="llm")
    sel = select_context("x", semantics=_schema(), config=cfg, llm=llm, model="m")
    assert sel.tables == ["orders"]
    assert any("unknown table" in n for n in sel.notes)


def test_llm_empty_result_falls_back_but_keeps_usage() -> None:
    llm = MockLLMClient([structured_reply({"tables": []}, usage=Usage(input_tokens=30))])
    cfg = SelectionConfig(strategy="llm")
    sel = select_context("x", semantics=_schema(), config=cfg, llm=llm, model="m")
    assert len(sel.tables) == 4  # fell back to include-all
    assert sel.fell_back is True  # and the degradation is a first-class, observable signal
    assert sel.strategy == "include_all"  # rewritten honestly, not claiming the narrow worked
    assert sel.usage.input_tokens == 30  # but we still spent (and metered) the call


def test_llm_without_client_degrades_to_lexical() -> None:
    cfg = SelectionConfig(strategy="llm", max_tables=2)
    sel = select_context("orders by customers", semantics=_schema(), config=cfg)
    assert set(sel.tables) == {"orders", "customers"}
    assert any("falling back to lexical" in n for n in sel.notes)


def test_llm_lexical_prefilters_the_catalog_before_asking() -> None:
    # The LLM only sees the lexical top-N. Assert the prompt didn't include the
    # clearly-irrelevant table so the catalog really was narrowed.
    captured: dict[str, str] = {}

    def _reply(req: object) -> object:
        captured["prompt"] = req.messages[0].content  # type: ignore[attr-defined]
        return structured_reply({"tables": ["orders"]})

    llm = MockLLMClient([_reply])
    cfg = SelectionConfig(strategy="llm_lexical", max_tables=1)
    sel = select_context("orders total", semantics=_schema(), config=cfg, llm=llm, model="m")
    assert sel.tables == ["orders"]
    assert "orders" in captured["prompt"]
    # inventory scores zero on "orders total", so the lexical prefilter drops it from the
    # catalog the LLM ever sees — the whole point of llm_lexical on a wide schema.
    assert "inventory" not in captured["prompt"]


# --- compiler integration --------------------------------------------------------


def test_compile_renders_only_selected_tables() -> None:
    cfg = SelectionConfig(strategy="lexical", max_tables=1)
    ctx = compile_context(
        "orders total", dialect=Dialect.duckdb, semantics=_schema(), selection=cfg
    )
    assert ctx.selected_tables == ["orders"]
    assert "## orders" in ctx.system
    assert "## inventory" not in ctx.system


def test_compile_keeps_only_examples_referencing_selected_tables() -> None:
    cfg = SelectionConfig(strategy="lexical", max_tables=1)
    examples = [
        Example(question="orders?", sql="SELECT count(*) FROM orders"),
        Example(question="stock?", sql="SELECT sum(on_hand) FROM inventory"),
    ]
    ctx = compile_context(
        "orders total",
        dialect=Dialect.duckdb,
        semantics=_schema(),
        examples=examples,
        selection=cfg,
    )
    assert "FROM orders" in ctx.system
    assert "FROM inventory" not in ctx.system


def test_compile_threads_selection_usage_onto_context() -> None:
    llm = MockLLMClient([structured_reply({"tables": ["orders"]}, usage=Usage(input_tokens=25))])
    cfg = SelectionConfig(strategy="llm")
    ctx = compile_context(
        "x", dialect=Dialect.duckdb, semantics=_schema(), selection=cfg, llm=llm, model="m"
    )
    assert ctx.usage.input_tokens == 25


def test_compile_surfaces_strategy_and_fallback() -> None:
    llm = MockLLMClient([structured_reply({"tables": []})])  # empty → fallback
    cfg = SelectionConfig(strategy="llm")
    ctx = compile_context(
        "x", dialect=Dialect.duckdb, semantics=_schema(), selection=cfg, llm=llm, model="m"
    )
    assert ctx.selection_fell_back is True
    assert ctx.selection_strategy == "include_all"
    assert any("matched no tables" in n for n in ctx.notes)


def test_compile_renders_value_hints() -> None:
    cfg = SelectionConfig(strategy="include_all", value_matching=True)
    ctx = compile_context(
        "sales in EMEA", dialect=Dialect.duckdb, semantics=_schema(), selection=cfg
    )
    assert "Value hints" in ctx.system
    assert "region" in ctx.system


def test_compile_on_llm_call_hook_fires_once() -> None:
    calls: list[tuple[object, object]] = []
    llm = MockLLMClient([structured_reply({"tables": ["orders"]})])
    cfg = SelectionConfig(strategy="llm")
    compile_context(
        "x",
        dialect=Dialect.duckdb,
        semantics=_schema(),
        selection=cfg,
        llm=llm,
        model="m",
        on_llm_call=lambda req, resp: calls.append((req, resp)),
    )
    assert len(calls) == 1
