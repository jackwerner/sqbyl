"""Selection eval — scoring the table-shortlister on its own (spec §5.1, plan 9.1).

Schema selection is a **first-class, separately-evaluable component**: a large-schema
agent can only get an answer right if selection first keeps the tables that answer
needs. This module scores exactly that, independent of whether the agent then writes
correct SQL, so a selection regression is legible before it shows up as a mysterious
accuracy drop.

**Ground truth comes from the gold SQL** — the tables a benchmark's gold query
references *are* the tables selection must keep. So there's no new label to maintain:
we derive the expected set from ``gold_sql`` (questions answered by a ``gold_asset``
are skipped, since their tables aren't spelled out inline).

The headline is **recall** — the fraction of questions where selection kept *every*
gold table — because a missed table makes the question unanswerable, while an extra
table only costs tokens. Compression (how much smaller the selected set is than the
whole schema) is reported alongside so you can see the tokens-saved/recall trade-off.

Dev-side (it reads benchmarks + runs the runtime selector), so it lives in ``sqbyl``.
It calls the runtime :func:`~sqbyl_runtime.selection.select_context` — the same code
the agent runs — never a reimplementation.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from pydantic import Field

from sqbyl.models.benchmarks import BenchmarkQuestion
from sqbyl.stats import wilson_interval
from sqbyl_runtime.context import ProjectKnowledge
from sqbyl_runtime.llm.base import LLMClient, Usage
from sqbyl_runtime.models import SqbylModel, TableSemantics
from sqbyl_runtime.selection import select_context


class SelectionEvalItem(SqbylModel):
    """One question's selection outcome versus the tables its gold SQL needs."""

    id: str
    expected: list[str]
    selected: list[str]
    missed: list[str]
    extra: list[str]

    @property
    def covered(self) -> bool:
        """Whether selection kept every table the gold answer needs (the thing that matters)."""
        return not self.missed


class SelectionEvalReport(SqbylModel):
    """Aggregate selection quality over a benchmark split.

    ``recall`` is over ``scored`` questions (those with inline ``gold_sql``); ``skipped``
    (``gold_asset``-backed) never enters the denominator, so **always read recall next to
    ``scored``/``skipped``** — a mostly-asset benchmark can score a confident recall on a
    tiny, unrepresentative subset. ``recall_interval`` gives the Wilson 95% bounds so a
    strategy change isn't judged on a within-noise delta on a tens-of-questions set
    (ml-systems). ``model`` stamps the selection model for an LLM strategy so a
    token-spending score is tied to what produced it; it's ``None`` for the deterministic
    strategies. ``fell_back`` counts questions where selection degraded to include-all —
    a high count means the selector is effectively off.
    """

    strategy: str
    items: list[SelectionEvalItem]
    scored: int
    skipped: int
    fell_back: int = 0
    model: str | None = None
    usage: Usage = Field(default_factory=Usage)

    @property
    def _covered(self) -> int:
        return sum(1 for it in self.items if it.covered)

    @property
    def recall(self) -> float:
        """Fraction of scored questions where every gold table survived selection.

        ``1.0`` when nothing was scored — vacuously true, so never read it without
        ``scored`` (a zero-denominator recall is not evidence of a good selector).
        """
        if not self.items:
            return 1.0
        return self._covered / len(self.items)

    @property
    def recall_interval(self) -> tuple[float, float]:
        """Wilson 95% bounds on ``recall`` — how much of a delta is signal vs. small-N noise."""
        return wilson_interval(self._covered, len(self.items))

    @property
    def mean_selected(self) -> float:
        """Average number of tables selected per question."""
        if not self.items:
            return 0.0
        return sum(len(it.selected) for it in self.items) / len(self.items)


def evaluate_selection(
    questions: Sequence[BenchmarkQuestion],
    *,
    semantics: Sequence[TableSemantics],
    knowledge: ProjectKnowledge,
    llm: LLMClient | None = None,
    model: str | None = None,
) -> SelectionEvalReport:
    """Score selection over ``questions`` using the project's own strategy/config.

    Runs the runtime selector once per question with a ``gold_sql`` and compares the
    tables it kept to the tables the gold SQL names. Questions defined by a
    ``gold_asset`` (no inline SQL) are skipped and counted. Any LLM tokens the strategy
    spends are summed onto ``report.usage`` so a caller can meter/budget them.

    For an ``llm``/``llm_lexical`` strategy this re-runs selection (it is not the exact
    call the agent eval made — temperature-0 is not a hard determinism guarantee), so the
    report is stamped with ``model``; scoring off the agent run's recorded
    ``selected_tables`` is a deferred follow-up. Deterministic strategies match the agent
    exactly.
    """
    table_names = [t.table for t in semantics]
    items: list[SelectionEvalItem] = []
    skipped = 0
    fell_back = 0
    total_usage = Usage()

    for q in questions:
        if q.gold_sql is None:
            skipped += 1
            continue
        expected = _gold_tables(q.gold_sql, table_names)
        picked = select_context(
            q.question,
            semantics=semantics,
            config=knowledge.selection,
            llm=llm,
            model=model,
        )
        total_usage = total_usage + picked.usage
        if picked.fell_back:
            fell_back += 1
        selected = set(picked.tables)
        expected_set = set(expected)
        items.append(
            SelectionEvalItem(
                id=q.id,
                expected=expected,
                selected=picked.tables,
                missed=sorted(expected_set - selected),
                extra=sorted(selected - expected_set),
            )
        )

    llm_strategy = knowledge.selection.strategy in ("llm", "llm_lexical")
    return SelectionEvalReport(
        strategy=knowledge.selection.strategy,
        items=items,
        scored=len(items),
        skipped=skipped,
        fell_back=fell_back,
        model=model if llm_strategy else None,
        usage=total_usage,
    )


def _gold_tables(gold_sql: str, table_names: Sequence[str]) -> list[str]:
    """The declared tables a gold query references, as whole words (case-insensitive).

    An **approximate, superset** label — a reasonable v1, not exact truth. It matches any
    declared table name appearing as a token in the SQL, after stripping ``--`` and ``/* */``
    comments so a name mentioned only in a comment doesn't count. Known biases: a CTE/subquery
    alias that happens to equal a declared table name over-counts, and a name inside a string
    literal still matches. Over-counting makes recall *pessimistic* (selection is dinged for a
    table the answer never truly needed), so read a low recall as "investigate", not "broken".
    It never invents a table that isn't in the schema (matches only ``table_names``).
    """
    sql = _strip_sql_comments(gold_sql)
    return [name for name in table_names if re.search(rf"\b{re.escape(name)}\b", sql, re.I)]


def _strip_sql_comments(sql: str) -> str:
    """Remove ``--`` line comments and ``/* */`` block comments before name matching."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql
