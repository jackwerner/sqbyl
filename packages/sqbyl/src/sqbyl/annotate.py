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

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from sqbyl.models.attention import Decision, DecisionKind
from sqbyl_runtime.llm.base import LLMClient, LLMRequest, LLMResponse, Message
from sqbyl_runtime.models import Column, Profile, TableSemantics
from sqbyl_runtime.state.traces import TraceWriter, llm_call_span, new_trace_id

_SYSTEM = (
    "You are a data analyst documenting a SQL table for a text-to-SQL agent. "
    "Write concise, factual descriptions grounded in the COLUMN PROFILE STATISTICS "
    "provided (ranges, distinct counts, sample values) — infer units and meaning from "
    "the data, never invent facts you can't support. "
    "When a column already has an EXISTING NOTE (from the database catalog or a human), treat "
    "it as authoritative: reconcile your description with it rather than contradicting it, and "
    "if the data genuinely conflicts with the note, say so plainly and lower your confidence "
    "instead of silently overriding it. "
    "Add a few natural-language synonyms a user might say. Give each description a confidence "
    "in [0,1]: high when the data (and any note) make the meaning obvious, low when the column "
    "is cryptic, coded, or you are guessing — a low confidence routes it to a human for review."
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

    @model_validator(mode="before")
    @classmethod
    def _unwrap_nested_shape(cls, data: Any) -> Any:
        """Accept the nested wrapper some models emit instead of the flat shape.

        ``claude-sonnet-5`` intermittently (content-dependent, deterministic at temp 0)
        returns the annotation wrapped in a single object field — e.g.
        ``{"table_description": {"description": ..., "confidence": ..., "columns": ...}}``
        — rather than the flat fields the schema declares. Unwrap that one level so a
        model's output shape doesn't abort the annotate run (finding B9). Only fires when
        the flat ``description`` is absent and a lone nested object plainly carries it, so
        a well-formed payload is never touched."""
        if isinstance(data, dict) and "description" not in data:
            nested = [v for v in data.values() if isinstance(v, dict) and "description" in v]
            if len(nested) == 1:
                return nested[0]
        return data


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
        "identifier",  # the long form of "id" — ID columns routinely list it, and two IDs
        # sharing "identifier" is never the disambiguation a human needs to see (finding #2 UX).
        "this",
        "value",
        "amount",
        "each",
        "with",
    }
)


def _topical_tokens(table_name: str) -> frozenset[str]:
    """The table's own entity words (``analytics.orders`` → ``{"orders", "order"}``).

    These are *topical*, not disambiguating: every column in an orders table is "about" orders,
    so a synonym that merely shares the entity root — ``order_id``'s "order number",
    ``order_date``'s "order date" — isn't a genuine ambiguity, it's the table's subject. Excluding
    them keeps the real signal (a contested term like "price") from being buried under one topical
    collision per column pair, which is what turned a 6-table schema into 38 warnings (finding #2
    UX). Includes a naive de-pluralization so the singular root matches column vocabulary."""
    leaf = table_name.rsplit(".", 1)[-1]
    tokens: set[str] = set()
    for tok in re.split(r"[^a-z0-9]+", leaf.lower()):
        if len(tok) >= 3:
            tokens.add(tok)
            if tok.endswith("s") and len(tok) > 3:
                tokens.add(tok[:-1])
    return frozenset(tokens)


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


def _detect_collisions(
    columns: list[tuple[str, list[str]]], *, topical: frozenset[str] = frozenset()
) -> list[SynonymCollision]:
    """Core detector over ``(name, synonyms)`` pairs — shared by the draft-time and
    written-file entry points below.

    A collision is a **synonym token** of column A that also appears in column B's own name or
    synonyms (or vice-versa): the word points at both columns, so which one the agent picks is
    a coin flip the metadata doesn't resolve. ``topical`` tokens (the table's own entity words —
    see :func:`_topical_tokens`) are excluded: they're the table's subject, not a real contest.
    Tokens shared across ``_GENERIC_TOKEN_MIN_DEGREE`` or more columns are dropped as generic
    table vocabulary rather than a two-way contest (finding B6). Deterministic and $0. Sorted,
    de-duplicated by (token, column-pair)."""
    syn_tokens = {name: _content_tokens(list(syns)) for name, syns in columns}
    id_tokens = {name: _content_tokens([name, *syns]) for name, syns in columns}

    # Degree of a token = how many columns' vocabulary (name + synonyms) it appears in. A word
    # spread across many columns is table vocabulary, not a contest between two of them.
    degree: dict[str, int] = {}
    for toks in id_tokens.values():
        for tok in toks:
            degree[tok] = degree.get(tok, 0) + 1

    out: list[SynonymCollision] = []
    seen: set[tuple[str, tuple[str, str]]] = set()
    for i, (a_name, _) in enumerate(columns):
        for b_name, _ in columns[i + 1 :]:
            shared = (syn_tokens[a_name] & id_tokens[b_name]) | (
                syn_tokens[b_name] & id_tokens[a_name]
            )
            for tok in sorted(shared - topical):
                if degree[tok] >= _GENERIC_TOKEN_MIN_DEGREE:
                    continue  # generic table vocabulary, not a resolvable pairwise contest
                pair = (a_name, b_name) if a_name <= b_name else (b_name, a_name)
                if (tok, pair) not in seen:
                    seen.add((tok, pair))
                    out.append(SynonymCollision(token=tok, columns=pair))
    return out


def detect_synonym_collisions(
    annotation: TableAnnotation, *, table_name: str | None = None
) -> list[SynonymCollision]:
    """Flag contested synonyms in a freshly-drafted :class:`TableAnnotation` (draft time).

    Pass ``table_name`` (the annotation object doesn't carry it) so the table's own entity words
    are treated as topical, not as collisions — the same de-noising the written-file scan gets."""
    topical = _topical_tokens(table_name) if table_name else frozenset()
    return _detect_collisions(
        [(c.name, list(c.synonyms)) for c in annotation.columns], topical=topical
    )


def detect_semantics_collisions(table: TableSemantics) -> list[SynonymCollision]:
    """Flag contested synonyms in an already-written :class:`TableSemantics` file — used to
    surface collisions after `init`'s parallel annotate wave (where the per-unit draft ran in
    a worker thread), by re-scanning the persisted columns. The table's own entity words are
    excluded as topical (finding #2 UX)."""
    return _detect_collisions(
        [(c.name, list(c.synonyms)) for c in table.columns],
        topical=_topical_tokens(table.table),
    )


# A token shared by this many columns (or more) is generic table vocabulary — a triplicated
# admin block (``AdmFName1/2/3``, ``AdmLName1/2/3``, ``AdmEmail1/2/3``) has every column
# answering to "administrator"/"name", so flagging all O(n²) pairs that share such a word
# buries the one real contest and would cap half the table's confidence (finding B6, BIRD
# `california_schools`). A genuine contest is two — occasionally a few — columns fighting over
# one word; past that it isn't a disambiguation a per-pair warning can resolve.
_GENERIC_TOKEN_MIN_DEGREE = 3


# A contested column can't be trusted enough to auto-apply, so its confidence is capped below
# the default auto-apply threshold (attention.AUTO_APPLY_THRESHOLD = 0.85) — the honest signal
# is "a human should look", not the locally-valid high confidence the per-table draft assigned.
_COLLISION_CONFIDENCE_CAP = 0.5


def flag_synonym_collisions(
    annotation: TableAnnotation, *, table_name: str | None = None
) -> tuple[TableAnnotation, list[SynonymCollision]]:
    """Detect collisions and return an annotation with contested columns' confidence capped.

    The cap keeps an overconfident-but-contested synonym from clearing the auto-apply gate
    (responsible-ai); the returned collisions are surfaced to the human. ``table_name`` excludes
    the table's own topical words from the contest. A collision-free annotation is returned
    unchanged."""
    collisions = detect_synonym_collisions(annotation, table_name=table_name)
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
    """Merge drafted descriptions/synonyms onto a table, preserving its profile — **fill-only**.

    Only ``description`` and ``synonyms`` are written (the durable authoring fields); profiling
    stays as profiled. Mirrors :func:`~sqbyl.semantics_io.merge_annotation`'s honesty rules
    (finding B11): a non-empty existing description is authoritative and never overwritten (the
    draft fills only a blank slot), and synonyms are unioned, not replaced. Columns are matched
    by name; unknown names are ignored."""
    by_name = {c.name: c for c in annotation.columns}
    columns = [
        col.model_copy(
            update={
                "description": _fill(col.description, by_name[col.name].description),
                "synonyms": _union(col.synonyms, by_name[col.name].synonyms),
            }
        )
        if col.name in by_name
        else col
        for col in table.columns
    ]
    return table.model_copy(
        update={
            "description": _fill(table.description, annotation.description),
            "synonyms": _union(table.synonyms, annotation.synonyms),
            "columns": columns,
        }
    )


def _fill(existing: str | None, draft: str) -> str | None:
    """Keep a non-empty existing description; only a blank slot is filled with the draft."""
    if existing and existing.strip():
        return existing
    return draft.strip() or None


def _union(existing: list[str], drafted: list[str]) -> list[str]:
    """Additive synonym union preserving order (existing first), deduped case-insensitively."""
    out: list[str] = []
    seen: set[str] = set()
    for s in [*existing, *drafted]:
        key = s.lower()
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


# Uncertainty routing (finding B11 / spec §5.5 "route attention"): a column the annotator can't
# ground confidently becomes a review-queue proposal, not a confident sentence written as truth.
def reconcile_annotation(
    table: TableSemantics, annotation: TableAnnotation, *, threshold: float
) -> tuple[TableAnnotation, list[Decision]]:
    """Reconcile a fresh draft against the table's existing (catalog/human) metadata.

    Returns ``(safe_annotation, decisions)``:

    * ``safe_annotation`` is what's safe to merge — an **un-described** column keeps its draft
      only when the annotator is confident (``confidence >= threshold``); a low-confidence draft
      is blanked so :func:`~sqbyl.semantics_io.merge_annotation` withholds it. Columns that
      already carry an authoritative note are blanked too (merge keeps the note either way; this
      makes the intent explicit).
    * ``decisions`` are the withheld low-confidence drafts, surfaced to the console review queue
      as pre-filled proposals a human accepts/edits — the "LLM proposes, human disposes" pattern
      the rest of sqbyl already follows, now applied to the build step.

    Existing notes are trusted over the model (inputs are truth), so a mere rewording of a note is
    never surfaced — only genuine gaps (un-described + uncertain) reach the queue, keeping the
    signal-to-noise high (the lesson from the synonym-collision work)."""
    profiles = {c.name: c.profile for c in table.columns}
    numeric_text = {n for n, p in profiles.items() if isinstance(p, Profile) and p.numeric_text}
    existing = {c.name: (c.description or "").strip() for c in table.columns}
    col_type = {c.name: c.type for c in table.columns}
    decisions: list[Decision] = []
    cols: list[ColumnAnnotation] = []
    for col in annotation.columns:
        has_note = bool(existing.get(col.name))
        draft = (col.description or "").strip()
        is_numtext = col.name in numeric_text
        # Withhold when un-described AND (uncertain, empty, OR numbers-stored-as-text). The last
        # is the key A4 case: a text column that's actually numeric mislabels *confidently*
        # ("area" for a population column), so its type mismatch always earns a human look even
        # when the model is sure — a bounded set (only numeric-text columns), not a queue flood.
        withhold = (not has_note) and (col.confidence < threshold or not draft or is_numtext)
        if withhold:
            note = _numeric_text_note(profiles.get(col.name)) if is_numtext else ""
            kind = col_type.get(col.name, "?")
            reason = (
                "type says text but values are numeric — confirm the meaning"
                if is_numtext
                else "the annotator was not confident"
            )
            decisions.append(
                Decision(
                    id=f"annotate:{table.table}:{col.name}",
                    kind=DecisionKind.column_description,
                    title=f"Describe {table.table}.{col.name}",
                    detail=f"No description yet; {reason} ({kind}).{note}",
                    suggestion=draft,
                    confidence=col.confidence,
                    source=f"annotate:{table.table}",
                )
            )
        # Keep the draft only for a confident, previously-undescribed column; blank it otherwise
        # so merge withholds it (uncertain) or keeps the authoritative note (already described).
        keep_draft = draft and not has_note and not withhold
        cols.append(col.model_copy(update={} if keep_draft else {"description": ""}))

    table_has_note = bool((table.description or "").strip())
    table_desc = annotation.description
    if table_has_note or annotation.confidence < threshold:
        table_desc = ""  # keep an existing note, or withhold a low-confidence table draft
        if not table_has_note and annotation.confidence < threshold:
            decisions.append(
                Decision(
                    id=f"annotate:{table.table}:__table__",
                    kind=DecisionKind.table_description,
                    title=f"Describe table {table.table}",
                    detail="No table description yet; the annotator was not confident.",
                    suggestion=annotation.description,
                    confidence=annotation.confidence,
                    source=f"annotate:{table.table}",
                )
            )
    return annotation.model_copy(update={"columns": cols, "description": table_desc}), decisions


def _numeric_text_note(profile: object) -> str:
    """Card detail for a numbers-stored-as-text column: name the CAST and, crucially, the
    magnitude — the range is what tells a reviewer 'population', not 'area in km²' (finding B12)."""
    rng = ""
    if isinstance(profile, Profile) and profile.min is not None and profile.max is not None:
        rng = f" (numeric range {profile.min}..{profile.max})"
    return f" Values are numbers stored as text{rng} — CAST before comparing/aggregating."


def save_annotation_review(path: Path, decisions: list[Decision]) -> None:
    """Persist a run's review proposals so the console queue can surface them ($0, local)."""
    payload = [d.model_dump(mode="json") for d in decisions]
    path.write_text(json.dumps(payload, indent=2) + "\n")


def load_annotation_review(path: Path) -> list[Decision]:
    """Load persisted annotate review proposals; empty when none have been written."""
    if not path.exists():
        return []
    return [Decision.model_validate(item) for item in json.loads(path.read_text())]


def _render_for_annotation(table: TableSemantics) -> str:
    """Expose the raw profile — plus any existing catalog/human notes — so the model grounds
    its descriptions in data and reconciles with (never discards) authoritative metadata."""
    lines = [f"Table: {table.table}"]
    if table.description:
        lines.append(f"Existing table note (authoritative): {table.description}")
    lines += ["", "Columns (name, type, profile):"]
    for col in table.columns:
        lines.append(f"- {col.name} ({col.type}){_profile_summary(col)}")
        if col.description:
            lines.append(f"    existing note (authoritative): {col.description}")
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
    p = col.profile if isinstance(col.profile, Profile) else None
    text_flag = (
        "  [stored as text but values are numeric]" if p is not None and p.numeric_text else ""
    )
    if col.sample_values:
        return f"  values: {', '.join(str(v) for v in col.sample_values)}{text_flag}"
    if p is not None:
        bits = []
        if p.nulls is not None:
            bits.append(f"nulls={p.nulls}")
        if p.distinct is not None:
            bits.append(f"distinct={p.distinct}")
        if p.min is not None and p.max is not None:
            bits.append(f"range={p.min}..{p.max}")
        if bits:
            return "  " + ", ".join(bits) + text_flag
    return text_flag
