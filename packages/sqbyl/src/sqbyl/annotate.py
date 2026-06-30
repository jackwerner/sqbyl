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
