"""Context-selection config (spec §5.1, §13).

For small projects the compiler includes everything; past ~30 tables Claude
shortlists relevant tables/examples from a compact catalog (LLM/lexical, never a
vector store). This config is embedded in a release so selection behaves the same
in production. The large-schema machinery itself is Phase 9; this just carries the
knobs.
"""

from __future__ import annotations

from typing import Literal

from sqbyl_runtime.models.base import SqbylModel

SelectionStrategy = Literal["include_all", "lexical", "llm", "llm_lexical"]


class SelectionConfig(SqbylModel):
    """How the context compiler narrows tables/examples for a question."""

    strategy: SelectionStrategy = "include_all"
    # Above this table count, "include everything" stops being viable (spec §13).
    max_tables: int | None = None
    # Lexically match high-cardinality terms to declared sample values ("EMEA" → region='emea').
    value_matching: bool = False
