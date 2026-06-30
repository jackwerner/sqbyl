"""Phase 2.1 — the context compiler (spec §5 steps 1-2).

Exit criterion: for the dogfood project, the compiled context is a deterministic,
snapshot-tested string given fixed inputs. Set ``SQBYL_UPDATE_SNAPSHOTS=1`` to
regenerate the golden file after an intentional change.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sqbyl.project import Project
from sqbyl.projectfiles import load_knowledge, parse_trusted_sql
from sqbyl_runtime.context import ProjectKnowledge, compile_context
from sqbyl_runtime.models import Dialect, TableSemantics

_SNAPSHOT = Path(__file__).resolve().parent / "snapshots" / "dogfood_context.txt"
_QUESTION = "What is net revenue by region?"


@pytest.fixture
def knowledge(dogfood_dir: Path) -> ProjectKnowledge:
    return load_knowledge(Project.load(dogfood_dir))


def test_trusted_sql_header_parsing(dogfood_dir: Path) -> None:
    asset = parse_trusted_sql((dogfood_dir / "trusted" / "mrr.sql").read_text())
    assert asset.name == "monthly_recurring_revenue"
    assert [(p.name, p.type) for p in asset.params] == [("month", "date")]
    assert asset.description and "Official" in asset.description
    assert asset.sql.upper().startswith("SELECT")
    assert "@name" not in asset.sql  # headers stripped from the body


def test_compiled_context_is_deterministic(knowledge: ProjectKnowledge) -> None:
    a = knowledge.compile(_QUESTION)
    b = knowledge.compile(_QUESTION)
    assert a == b


def test_selection_includes_everything(knowledge: ProjectKnowledge) -> None:
    ctx = knowledge.compile(_QUESTION)
    assert set(ctx.selected_tables) == {"analytics.orders", "analytics.customers"}
    assert ctx.offered_assets == ["monthly_recurring_revenue"]
    assert ctx.notes == []  # two tables is well under the include-all limit


def test_compiled_context_grounding(knowledge: ProjectKnowledge) -> None:
    system = knowledge.compile(_QUESTION).system
    # The semantic layer's leverage points all reach the prompt.
    assert "measure net_revenue:" in system
    assert "Trusted assets" in system and "monthly_recurring_revenue(month date)" in system
    assert "[values: confirmed, partial_refund, refunded]" in system  # profile sample values
    assert "[range: 127..310229]" in system  # profile numeric range
    assert "# Examples" in system
    # The question lands in the (varying) user turn, leaving the system block stable
    # for prompt caching.
    assert _QUESTION in knowledge.compile(_QUESTION).user
    other = knowledge.compile("How many customers are on the pro plan?")
    assert other.system == system  # system block is question-independent (cacheable)


def test_large_schema_emits_a_note() -> None:
    # Minimal tables past the include-all limit.
    many = [TableSemantics(table=f"s.t{i}") for i in range(31)]
    ctx = compile_context("q", dialect=Dialect.duckdb, semantics=many)
    assert any("Phase 9" in n for n in ctx.notes)
    assert len(ctx.selected_tables) == 31  # still include everything for now


def test_matches_golden_snapshot(knowledge: ProjectKnowledge) -> None:
    system = knowledge.compile(_QUESTION).system
    if os.environ.get("SQBYL_UPDATE_SNAPSHOTS"):
        _SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        _SNAPSHOT.write_text(system)
    assert _SNAPSHOT.exists(), "run with SQBYL_UPDATE_SNAPSHOTS=1 to create the golden file"
    assert system == _SNAPSHOT.read_text(), (
        "compiled context drifted — review the change and re-run with "
        "SQBYL_UPDATE_SNAPSHOTS=1 if intended"
    )
