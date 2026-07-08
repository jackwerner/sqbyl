"""The annotator — ``sqbyl annotate`` (spec §3 #1, plan 2.3).

Claude drafts table/column **descriptions and synonyms**, grounded in the Phase 1.3
**profile** (ranges, distinct counts, sample values) rather than guessing from
names — it can see that ``amount_cents`` spans 127..310229 with no nulls and infer
the cents unit, that ``status`` has three values, that ``created_at`` covers
2019→today. Each annotation carries a **confidence** the attention router consumes
later (Phase 6).

This is dev-only authoring, so it lives in ``sqbyl`` and depends on the runtime's
LLM seam. It is per-table (parallel fan-out is Phase 6; here it is sequential). As
a paid command it is metered through the cost stub from day one (invariant 5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

from sqbyl_runtime.llm.base import LLMClient, LLMRequest, LLMResponse, Message
from sqbyl_runtime.models import Column, Profile, TableSemantics
from sqbyl_runtime.state.traces import TraceWriter, llm_call_span, new_trace_id

_SYSTEM = (
    "You are a data analyst documenting a SQL table for a text-to-SQL agent. "
    "Write concise, factual descriptions grounded in the COLUMN PROFILE STATISTICS "
    "provided (ranges, distinct counts, sample values) — infer units and meaning from "
    "the data, never invent facts you can't support. Add a few natural-language "
    "synonyms a user might say. Give each description a confidence in [0,1]: high when "
    "the data makes the meaning obvious, low when the column is cryptic."
)


class ColumnAnnotation(BaseModel):
    """A drafted description for one column, with the router's confidence signal."""

    name: str
    description: str = Field(description="One factual sentence, grounded in the profile.")
    synonyms: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class TableAnnotation(BaseModel):
    """A drafted description for a table and its columns."""

    description: str = Field(description="What one row represents, grounded in the data.")
    synonyms: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    columns: list[ColumnAnnotation] = Field(default_factory=list)


def annotate_table(
    llm: LLMClient,
    table: TableSemantics,
    *,
    model: str,
    trace_writer: TraceWriter | None = None,
    trace_id: str | None = None,
    parent_span_id: str | None = None,
) -> tuple[TableAnnotation, LLMResponse]:
    """Draft a description/synonyms for a table and its columns, grounded in the profile.

    Like the agent pipeline, this builds the request explicitly so the token-spending
    call can be written as an OTel-GenAI span when a ``trace_writer`` is given
    (invariant 7). ``max_tokens`` matches the seam's structured default so existing
    cassettes stay valid.
    """
    request = LLMRequest(
        model=model,
        messages=[Message(role="user", content=_render_for_annotation(table))],
        system=_SYSTEM,
        response_schema=TableAnnotation.model_json_schema(),
        max_tokens=4096,
        temperature=0.0,
        cache_system=True,
    )
    response = llm.complete(request)
    if trace_writer is not None:
        trace_writer.write(
            llm_call_span(
                request,
                response,
                operation="chat",
                name=f"annotate {table.table}",
                trace_id=trace_id or new_trace_id(),
                parent_span_id=parent_span_id,
            )
        )
    return response.parse(TableAnnotation), response


# --- synonym-collision detection (finding #2): a $0, deterministic pass ---------------------
#
# The annotator drafts each column in isolation (grounded only in that column's own profile),
# so it can confidently give one column a synonym that *equally* describes a sibling — e.g.
# "cost" on ``cost_price`` while ``unit_price`` is the price a user asking about "cost" often
# means. The result reads as a clean, high-confidence synonyms list that hides a contested
# term, and the agent then silently routes the word to one column. This pass flags that
# ambiguity from shared vocabulary — no LLM, no tokens — so a human sees the contest the
# per-table draft can't (spec §3 #1 "route attention"; responsible-ai: don't let an
# overconfident signal auto-apply a decision it can't actually make).

# Generic words that don't disambiguate a column, so a shared occurrence isn't a real collision.
_COLLISION_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "per",
        "by",
        "and",
        "or",
        "to",
        "in",
        "on",
        "for",
        "id",
        "this",
        "value",
        "amount",
        "each",
        "with",
    }
)


def _content_tokens(phrases: list[str]) -> set[str]:
    """Lowercased content tokens (≥3 chars, non-stopword) across ``phrases`` — the vocabulary
    a user might say to mean a column. ``"purchase price"`` → ``{"purchase", "price"}``."""
    tokens: set[str] = set()
    for phrase in phrases:
        for tok in re.split(r"[^a-z0-9]+", phrase.lower()):
            if len(tok) >= 3 and tok not in _COLLISION_STOPWORDS:
                tokens.add(tok)
    return tokens


@dataclass(frozen=True)
class SynonymCollision:
    """A word two columns in the same table both plausibly answer to — a contested synonym."""

    token: str
    columns: tuple[str, str]  # the two colliding column names, sorted

    def describe(self) -> str:
        a, b = self.columns
        return (
            f"'{self.token}' is shared vocabulary for both {a} and {b} — a user saying "
            f"'{self.token}' could mean either; add a disambiguating description to each"
        )


def _detect_collisions(columns: list[tuple[str, list[str]]]) -> list[SynonymCollision]:
    """Core detector over ``(name, synonyms)`` pairs — shared by the draft-time and
    written-file entry points below.

    A collision is a **synonym token** of column A that also appears in column B's own name or
    synonyms (or vice-versa): the word points at both columns, so which one the agent picks is
    a coin flip the metadata doesn't resolve. Deterministic and $0. Sorted, de-duplicated by
    (token, column-pair)."""
    syn_tokens = {name: _content_tokens(list(syns)) for name, syns in columns}
    id_tokens = {name: _content_tokens([name, *syns]) for name, syns in columns}
    out: list[SynonymCollision] = []
    seen: set[tuple[str, tuple[str, str]]] = set()
    for i, (a_name, _) in enumerate(columns):
        for b_name, _ in columns[i + 1 :]:
            shared = (syn_tokens[a_name] & id_tokens[b_name]) | (
                syn_tokens[b_name] & id_tokens[a_name]
            )
            for tok in sorted(shared):
                pair = (a_name, b_name) if a_name <= b_name else (b_name, a_name)
                if (tok, pair) not in seen:
                    seen.add((tok, pair))
                    out.append(SynonymCollision(token=tok, columns=pair))
    return out


def detect_synonym_collisions(annotation: TableAnnotation) -> list[SynonymCollision]:
    """Flag contested synonyms in a freshly-drafted :class:`TableAnnotation` (draft time)."""
    return _detect_collisions([(c.name, list(c.synonyms)) for c in annotation.columns])


def detect_semantics_collisions(table: TableSemantics) -> list[SynonymCollision]:
    """Flag contested synonyms in an already-written :class:`TableSemantics` file — used to
    surface collisions after `init`'s parallel annotate wave (where the per-unit draft ran in
    a worker thread), by re-scanning the persisted columns."""
    return _detect_collisions([(c.name, list(c.synonyms)) for c in table.columns])


# A contested column can't be trusted enough to auto-apply, so its confidence is capped below
# the default auto-apply threshold (attention.AUTO_APPLY_THRESHOLD = 0.85) — the honest signal
# is "a human should look", not the locally-valid high confidence the per-table draft assigned.
_COLLISION_CONFIDENCE_CAP = 0.5


def flag_synonym_collisions(
    annotation: TableAnnotation,
) -> tuple[TableAnnotation, list[SynonymCollision]]:
    """Detect collisions and return an annotation with contested columns' confidence capped.

    The cap keeps an overconfident-but-contested synonym from clearing the auto-apply gate
    (responsible-ai); the returned collisions are surfaced to the human. A collision-free
    annotation is returned unchanged."""
    collisions = detect_synonym_collisions(annotation)
    if not collisions:
        return annotation, collisions
    contested = {name for c in collisions for name in c.columns}
    columns = [
        col.model_copy(update={"confidence": min(col.confidence, _COLLISION_CONFIDENCE_CAP)})
        if col.name in contested
        else col
        for col in annotation.columns
    ]
    return annotation.model_copy(update={"columns": columns}), collisions


def apply_annotation(table: TableSemantics, annotation: TableAnnotation) -> TableSemantics:
    """Merge drafted descriptions/synonyms onto a table, preserving its profile.

    Only ``description`` and ``synonyms`` are written (the durable authoring fields);
    profiling stays as profiled. Columns are matched by name; unknown names are ignored.
    """
    by_name = {c.name: c for c in annotation.columns}
    columns = [
        col.model_copy(
            update={
                "description": by_name[col.name].description,
                "synonyms": by_name[col.name].synonyms,
            }
        )
        if col.name in by_name
        else col
        for col in table.columns
    ]
    return table.model_copy(
        update={
            "description": annotation.description,
            "synonyms": annotation.synonyms,
            "columns": columns,
        }
    )


def _render_for_annotation(table: TableSemantics) -> str:
    """Expose the raw profile so the model grounds its descriptions in data."""
    lines = [f"Table: {table.table}", "", "Columns (name, type, profile):"]
    for col in table.columns:
        lines.append(f"- {col.name} ({col.type}){_profile_summary(col)}")
    if table.joins:
        lines.append("")
        lines.append("Joins:")
        for join in table.joins:
            lines.append(f"- {join.type} -> {join.to} ON {join.on}")
    lines.append("")
    lines.append(
        "Draft a table description (what one row is) plus a description and synonyms for "
        "every column, each with a confidence. Return them all."
    )
    return "\n".join(lines)


def _profile_summary(col: Column) -> str:
    if col.sample_values:
        return f"  values: {', '.join(str(v) for v in col.sample_values)}"
    if isinstance(col.profile, Profile):
        p = col.profile
        bits = []
        if p.nulls is not None:
            bits.append(f"nulls={p.nulls}")
        if p.distinct is not None:
            bits.append(f"distinct={p.distinct}")
        if p.min is not None and p.max is not None:
            bits.append(f"range={p.min}..{p.max}")
        if bits:
            return "  " + ", ".join(bits)
    return ""
