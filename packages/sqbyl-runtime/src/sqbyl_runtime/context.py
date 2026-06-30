"""The context compiler (spec §5 steps 1-2, plan 2.1).

Turn a project's knowledge (semantics + examples + trusted assets + instructions)
plus a question into the prompt the agent runtime sends to Claude. Two halves:

* a **stable system block** — instructions, annotated schema/semantics, trusted
  assets, and few-shot examples. It does not vary with the question, so it is the
  prompt-cache unit (``cache_system=True`` downstream).
* a **question turn** — the one varying part, sent as the user message.

This lives in ``sqbyl-runtime`` because ``ask()`` compiles context at inference
time (the same compiler runs in dev and in a shipped release). The small-project
path is "include everything"; large-schema shortlisting is deferred to Phase 9 —
here we only emit a note past a sane table count.

The output is a pure function of its inputs (deterministic, snapshot-testable):
no clocks, no IDs, stable ordering.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, Field

from sqbyl_runtime.models import (
    Column,
    Dialect,
    Example,
    Profile,
    ReleaseArtifact,
    ScalarBound,
    SelectionConfig,
    TableSemantics,
    TrustedAsset,
)

# Past this many tables "include everything" stops being viable and Phase 9's
# LLM/lexical shortlisting is needed; until then we still include all but flag it.
_INCLUDE_ALL_TABLE_LIMIT = 30


class CompiledContext(BaseModel):
    """The compiled prompt plus what went into it (for citation + caching)."""

    system: str
    user: str
    selected_tables: list[str] = Field(default_factory=list)
    offered_assets: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ProjectKnowledge(BaseModel):
    """Everything the agent reasons over, decoupled from where it came from.

    Both a dev project (loaded from files) and a shipped ``ReleaseArtifact`` produce
    one of these, so the runtime pipeline is identical in dev and in production.
    """

    dialect: Dialect
    semantics: list[TableSemantics] = Field(default_factory=list)
    instructions: str = ""
    examples: list[Example] = Field(default_factory=list)
    trusted_assets: list[TrustedAsset] = Field(default_factory=list)
    selection: SelectionConfig = Field(default_factory=SelectionConfig)

    @classmethod
    def from_release(cls, release: ReleaseArtifact) -> ProjectKnowledge:
        return cls(
            dialect=release.dialect,
            semantics=release.semantics,
            instructions=release.instructions,
            examples=release.examples,
            trusted_assets=release.trusted_assets,
            selection=release.selection,
        )

    def compile(self, question: str) -> CompiledContext:
        return compile_context(
            question,
            dialect=self.dialect,
            semantics=self.semantics,
            instructions=self.instructions,
            examples=self.examples,
            trusted_assets=self.trusted_assets,
            selection=self.selection,
        )


def compile_context(
    question: str,
    *,
    dialect: Dialect,
    semantics: Sequence[TableSemantics],
    instructions: str = "",
    examples: Sequence[Example] = (),
    trusted_assets: Sequence[TrustedAsset] = (),
    selection: SelectionConfig | None = None,
) -> CompiledContext:
    """Compile the question + project knowledge into a system/user prompt pair."""
    selection = selection or SelectionConfig()
    notes: list[str] = []

    # Small-project path: include everything (spec §5.1). Large-schema selection is
    # Phase 9; for now we keep every table but flag when include-all gets unwieldy.
    selected = list(semantics)
    if len(selected) > _INCLUDE_ALL_TABLE_LIMIT:
        notes.append(
            f"{len(selected)} tables exceeds the include-everything limit "
            f"({_INCLUDE_ALL_TABLE_LIMIT}); large-schema selection lands in Phase 9"
        )

    system = _render_system(
        dialect=dialect,
        instructions=instructions,
        semantics=selected,
        trusted_assets=trusted_assets,
        examples=examples,
    )
    return CompiledContext(
        system=system,
        user=_render_question(question),
        selected_tables=[t.table for t in selected],
        offered_assets=[a.name for a in trusted_assets],
        notes=notes,
    )


# --- rendering (deterministic) --------------------------------------------------


def _render_system(
    *,
    dialect: Dialect,
    instructions: str,
    semantics: Sequence[TableSemantics],
    trusted_assets: Sequence[TrustedAsset],
    examples: Sequence[Example],
) -> str:
    blocks: list[str] = [
        f"You are a careful {dialect.value} SQL analyst. Answer questions by writing a "
        "single read-only SELECT, grounded in the semantic layer below. Prefer measures, "
        "filters, and trusted assets over ad-hoc arithmetic.",
    ]
    if instructions.strip():
        # Rendered verbatim: instructions.md is author-owned markdown (it brings its
        # own headings), so we don't wrap it in another.
        blocks.append(instructions.strip())
    blocks.append(_render_tables(semantics))
    if trusted_assets:
        blocks.append(_render_trusted_assets(trusted_assets))
    if examples:
        blocks.append(_render_examples(examples))
    return "\n\n".join(blocks).strip() + "\n"


def _render_tables(semantics: Sequence[TableSemantics]) -> str:
    lines = ["# Schema"]
    for table in semantics:
        header = f"## {table.table}"
        if table.description:
            header += f" — {table.description}"
        lines.append(header)
        if table.synonyms:
            lines.append(f"synonyms: {', '.join(table.synonyms)}")
        lines.append("columns:")
        for col in table.columns:
            lines.append(_render_column(col))
        for join in table.joins:
            conf = "" if join.confidence is None else f"  [confidence {join.confidence:.2f}]"
            lines.append(f"join: {join.type} -> {join.to} ON {join.on}{conf}")
        for measure in table.measures:
            desc = f"  — {measure.description}" if measure.description else ""
            lines.append(f"measure {measure.name}: {measure.sql}{desc}")
        for filt in table.filters:
            desc = f"  — {filt.description}" if filt.description else ""
            lines.append(f"filter {filt.name}: {filt.sql}{desc}")
    return "\n".join(lines)


def _render_column(col: Column) -> str:
    parts = [f"- {col.name} ({col.type})"]
    if col.description:
        parts.append(f" — {col.description}")
    if col.synonyms:
        parts.append(f" [synonyms: {', '.join(col.synonyms)}]")
    hint = _profile_hint(col.profile, col.sample_values)
    if hint:
        parts.append(f" {hint}")
    return "".join(parts)


def _profile_hint(profile: Profile | bool | None, sample_values: list[ScalarBound] | None) -> str:
    """A compact grounding hint a human would eyeball: range or representative values."""
    if sample_values:
        shown = ", ".join(str(v) for v in sample_values)
        return f"[values: {shown}]"
    if isinstance(profile, Profile) and profile.min is not None and profile.max is not None:
        return f"[range: {profile.min}..{profile.max}]"
    return ""


def _render_trusted_assets(assets: Sequence[TrustedAsset]) -> str:
    lines = ["# Trusted assets (prefer these over ad-hoc SQL; cite the one you use)"]
    for asset in assets:
        params = ", ".join(f"{p.name} {p.type}" for p in asset.params)
        header = f"## {asset.name}({params})"
        if asset.description:
            header += f" — {asset.description}"
        lines.append(header)
        lines.append(asset.sql.strip())
    return "\n".join(lines)


def _render_examples(examples: Sequence[Example]) -> str:
    lines = ["# Examples"]
    for ex in examples:
        lines.append(f"Q: {ex.question}")
        lines.append("SQL:")
        lines.append(ex.sql.strip())
    return "\n".join(lines)


def _render_question(question: str) -> str:
    return (
        f"Question: {question.strip()}\n\n"
        "Write a single read-only SELECT that answers it. Think briefly about which "
        "tables, measures, and trusted assets apply, then give the SQL."
    )
