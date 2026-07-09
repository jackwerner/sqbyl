"""The stateless agent pipeline — ``ask()`` (spec §5 steps 3-7, plan 2.2).

One ``ask()`` is a stateless pipeline: **generate** (a short plan + candidate SQL,
via strict structured output) → **static-validate** (``EXPLAIN``, no execution) →
**execute** (read-only) → **self-repair** (feed the error back, up to N times) →
**respond** ``{plan, sql, rows, used_assets, usage, latency}`` → *(opt-in)* **narrate**
(one grounded summarization call turning the executed rows into a plain-English
``answer``).

This lives in ``sqbyl-runtime`` so the shipped "model with logs" is correct from day
one: the runtime owns generation, validation, read-only execution, repair, and the
OTel-shaped trace every run writes. Everything that *improves* the agent (eval,
coach, …) lives in ``sqbyl`` and is built on top of this.

Multi-turn is just a thread of these with prior turns prepended — not modeled here.
"""

from __future__ import annotations

import json
import time
from typing import Any

from pydantic import BaseModel, Field, ValidationError, model_validator

from sqbyl_runtime.context import CompiledContext, ProjectKnowledge
from sqbyl_runtime.db import (
    Database,
    QueryResult,
    StaticValidationError,
    UnparseableSqlError,
    WriteAttemptError,
)
from sqbyl_runtime.llm.base import LLMClient, LLMRequest, LLMResponse, Message, Usage
from sqbyl_runtime.state.traces import Span, TraceWriter, llm_call_span, new_span_id, new_trace_id

# OTel GenAI operation name for an agent invocation.
_GEN_AI_OPERATION = "chat"

# How many executed rows to hand the (opt-in) narrator. The narrated sentence is a
# convenience over the authoritative rows, so a big result set doesn't need to be
# streamed into a second paid call — cap it and note the truncation in the prompt so the
# model never implies it summarized rows it didn't see.
_NARRATE_ROW_CAP = 50

# The narrator is deliberately constrained: it reports the executed table and nothing
# else. Kept blunt so the summary can never introduce a number the SQL didn't produce
# (examples > semantics > prose; the rows remain the source of truth).
_NARRATE_SYSTEM = (
    "You restate the result of a SQL query as one short, plain-English sentence that "
    "answers the user's question. Use ONLY the values in the result table below — never "
    "invent, infer, or round beyond what is shown, and never add facts that are not in the "
    "rows. If the result is empty, say that no matching rows were found. Answer in one or "
    "two sentences and nothing else; the table is the source of truth."
)


class AgentGeneration(BaseModel):
    """The strict-JSON object the model emits each attempt."""

    plan: str = Field(
        description="A short chain-of-thought plan: which tables/measures/assets apply."
    )
    sql: str = Field(description="A single read-only SELECT that answers the question.")
    used_assets: list[str] = Field(
        default_factory=list,
        description="Names of any trusted assets you relied on (for citation). Empty if none.",
    )

    @model_validator(mode="before")
    @classmethod
    def _unwrap_nested_shape(cls, data: Any) -> Any:
        """Accept a one-level nested wrapper some models emit instead of the flat shape.

        A current, stronger model can wrap the object one level — e.g.
        ``{"agent_generation": {"plan": ..., "sql": ...}}`` — rather than the flat fields the
        schema declares. Unwrap that so a portability quirk doesn't force a needless repair
        round (mirrors the annotator's B9 fix). A near-miss the wrapper can't fix — a payload
        that genuinely omits ``sql`` — still raises, and the generate loop routes it into
        self-repair (finding B10). Only fires when the flat fields are absent and a lone nested
        object carries them, so a well-formed payload is untouched."""
        if isinstance(data, dict) and "sql" not in data and "plan" not in data:
            nested = [v for v in data.values() if isinstance(v, dict) and "sql" in v]
            if len(nested) == 1:
                return nested[0]
        return data


class AgentAttempt(BaseModel):
    """One generate→validate→execute cycle, kept for transparency/debugging."""

    sql: str
    plan: str
    error: str | None = None


class AgentResult(BaseModel):
    """The result of one ``ask()`` (spec §5 step 7)."""

    question: str
    plan: str
    sql: str
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    used_assets: list[str] = Field(default_factory=list)
    selected_tables: list[str] = Field(default_factory=list)
    # Why the schema was narrowed the way it was: the strategy that actually ran, whether it
    # degraded to include-all, and the selector's notes — so a question that silently lost the
    # table it needed (or a selector that's effectively off) is legible on the result, not only
    # in the raw trace (transparency; ml-systems / responsible-ai).
    selection_strategy: str = "include_all"
    selection_fell_back: bool = False
    selection_notes: list[str] = Field(default_factory=list)
    attempts: int = 0
    repaired: bool = False
    error: str | None = None
    usage: Usage = Field(default_factory=Usage)
    # Tokens spent by the opt-in narration call, kept *separate* from ``usage`` (which stays
    # the agent+selection spend) so it meters under its own role/model per invariant 5. Empty
    # unless ``narrate`` was requested and the query succeeded; ``total_usage`` sums the two.
    narration_usage: Usage = Field(default_factory=Usage)
    latency_ms: float = 0.0
    trace_id: str = ""
    # The model that produced this answer — stamped so a production caller logging the result
    # has per-answer provenance (which model), not just the config that was live at load time.
    model: str = ""
    # Opt-in plain-English restatement of ``rows`` (off by default). A convenience layer over
    # the authoritative ``columns``/``rows``, which remain the source of truth — never trust a
    # figure that appears here but not in the rows. ``None`` when narration wasn't requested,
    # or when the query failed (there's nothing grounded to summarize).
    answer: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def total_usage(self) -> Usage:
        """Grand-total tokens for this run: agent+selection (``usage``) plus any narration."""
        return self.usage + self.narration_usage


def ask(
    question: str,
    *,
    knowledge: ProjectKnowledge,
    db: Database,
    llm: LLMClient,
    model: str,
    selection_model: str | None = None,
    self_repair_attempts: int = 2,
    max_tokens: int = 2048,
    narrate: bool = False,
    narration_model: str | None = None,
    narration_max_tokens: int = 512,
    trace_writer: TraceWriter | None = None,
) -> AgentResult:
    """Answer ``question`` against ``db`` using ``knowledge`` and ``llm``.

    Generates, statically validates, executes read-only, and self-repairs up to
    ``self_repair_attempts`` times. Always writes an OTel-shaped run span (plus a
    child span per LLM call) when a ``trace_writer`` is given. ``selection_model``
    pins the model for large-schema table shortlisting (spec §5.1); it defaults to
    ``model`` so small projects — which never make a selection call — are unaffected.

    Narration is **opt-in and off by default** (so the ``$0``-by-default, deterministic
    posture is unchanged): with ``narrate=True`` a single extra summarization call — on
    ``narration_model`` (defaults to ``model``), grounded strictly on the executed
    ``columns``/``rows`` — populates ``result.answer`` with a plain-English sentence. It
    runs only when the query succeeded, is traced as its own GenAI child span, and its
    tokens land in ``result.narration_usage`` (metered as a distinct role, invariant 5).
    """
    started = time.perf_counter()
    trace_id = new_trace_id()
    run_span = Span(
        name="ask",
        trace_id=trace_id,
        span_id=new_span_id(),
        attributes={"gen_ai.operation.name": _GEN_AI_OPERATION, "sqbyl.question": question},
    )

    # Selection (large-schema shortlisting) may spend tokens; compile threads that usage
    # onto the context so it's metered with generation below (invariant 5). When the
    # strategy makes an LLM call, trace it as its own GenAI child span (invariant 7).
    def _trace_selection(req: LLMRequest, resp: LLMResponse) -> None:
        if trace_writer is not None:
            trace_writer.write(
                llm_call_span(
                    req,
                    resp,
                    operation=_GEN_AI_OPERATION,
                    name="select tables",
                    trace_id=trace_id,
                    parent_span_id=run_span.span_id,
                )
            )

    context = knowledge.compile(
        question,
        llm=llm,
        model=selection_model or model,
        on_llm_call=_trace_selection,
    )
    # Put the selection outcome on the run span so a silent drop/fallback is auditable in
    # the trace even on the deterministic paths that emit no child span (transparency).
    run_span.attributes["sqbyl.selection.strategy"] = context.selection_strategy
    run_span.attributes["sqbyl.selection.fell_back"] = context.selection_fell_back
    run_span.attributes["sqbyl.selection.selected_tables"] = context.selected_tables
    if context.notes:
        run_span.attributes["sqbyl.selection.notes"] = context.notes

    messages: list[Message] = [Message(role="user", content=context.user)]
    # Seed the run's usage with any tokens the selection step already spent so the
    # returned total (and thus the meter/budget) accounts for shortlisting, not just
    # generation.
    total_usage = context.usage
    attempts: list[AgentAttempt] = []
    last_gen: AgentGeneration | None = None

    # attempt 0 is the first try; each subsequent attempt is a self-repair.
    for attempt_index in range(self_repair_attempts + 1):
        request = LLMRequest(
            model=model,
            messages=messages,
            system=context.system,
            response_schema=AgentGeneration.model_json_schema(),
            max_tokens=max_tokens,
            temperature=0.0,
            cache_system=True,
        )
        response = llm.complete(request)
        total_usage = total_usage + response.usage
        if trace_writer is not None:
            trace_writer.write(
                llm_call_span(
                    request,
                    response,
                    operation=_GEN_AI_OPERATION,
                    name=f"generate (attempt {attempt_index + 1})",
                    trace_id=trace_id,
                    parent_span_id=run_span.span_id,
                )
            )
        try:
            gen = response.parse(AgentGeneration)
        except (ValidationError, ValueError) as exc:
            # A malformed generation — e.g. a model that omits the required `sql` field — is a
            # failed attempt that feeds self-repair, never an exception that propagates out of
            # ask() and aborts an entire eval run (finding B10; the same posture as the
            # SQL-validate guard below / B4, at the parse step one level up).
            parse_error = f"the response did not match the required answer schema: {exc}"
            attempts.append(AgentAttempt(sql="", plan="", error=parse_error))
            raw = response.text or json.dumps(response.structured or {})
            messages.append(Message(role="assistant", content=raw))
            messages.append(Message(role="user", content=_reparse_prompt(parse_error)))
            continue
        last_gen = gen

        rows, error = _validate_and_execute(db, gen.sql)
        if error is None:
            assert rows is not None
            row_lists = [list(r) for r in rows.rows]
            # Opt-in: turn the executed rows into a plain-English sentence. Metered apart from
            # the agent spend (its own role/model, invariant 5) and traced as its own span.
            answer_text: str | None = None
            narration_usage = Usage()
            if narrate:
                answer_text, narration_usage = _narrate(
                    question,
                    rows.columns,
                    row_lists,
                    llm=llm,
                    model=narration_model or model,
                    max_tokens=narration_max_tokens,
                    trace_writer=trace_writer,
                    trace_id=trace_id,
                    parent_span_id=run_span.span_id,
                )
                run_span.attributes["sqbyl.narrated"] = True
            result = _success(
                question,
                gen,
                context,
                rows_columns=rows.columns,
                rows=row_lists,
                attempts=attempt_index + 1,
                usage=total_usage,
                narration_usage=narration_usage,
                answer=answer_text,
                latency_ms=_elapsed_ms(started),
                trace_id=trace_id,
                model=model,
            )
            _finish(trace_writer, run_span, status="ok")
            return result

        attempts.append(AgentAttempt(sql=gen.sql, plan=gen.plan, error=error))
        # Thread the failure back in for the next attempt (keeps role alternation).
        messages.append(Message(role="assistant", content=gen.sql))
        messages.append(Message(role="user", content=_repair_prompt(error)))

    # Exhausted all repair attempts.
    _finish(trace_writer, run_span, status="error")
    # ``last_gen`` is None only when *every* attempt failed to even parse (finding B10): there's
    # no SQL to report, but the question still gets a clean error verdict, never an exception.
    return AgentResult(
        question=question,
        plan=last_gen.plan if last_gen is not None else "",
        sql=last_gen.sql if last_gen is not None else "",
        used_assets=last_gen.used_assets if last_gen is not None else [],
        selected_tables=context.selected_tables,
        selection_strategy=context.selection_strategy,
        selection_fell_back=context.selection_fell_back,
        selection_notes=context.notes,
        attempts=len(attempts),
        repaired=len(attempts) > 1,
        error=attempts[-1].error if attempts else "the model produced no parseable answer",
        usage=total_usage,
        latency_ms=_elapsed_ms(started),
        trace_id=trace_id,
        model=model,
    )


def _validate_and_execute(db: Database, sql: str) -> tuple[QueryResult | None, str | None]:
    """Static-validate (``EXPLAIN``) then execute; return ``(rows, None)`` on success
    or ``(None, error)`` so the caller can self-repair without re-running the query."""
    try:
        db.explain(sql)
    except (StaticValidationError, UnparseableSqlError, WriteAttemptError) as exc:
        # An unparseable generation is a wrong answer that feeds self-repair, not a
        # crash that aborts the whole eval run.
        return None, str(exc)
    try:
        return db.execute(sql), None
    except WriteAttemptError as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 - any DB execution error feeds self-repair
        return None, str(exc)


def _success(
    question: str,
    gen: AgentGeneration,
    context: CompiledContext,
    *,
    rows_columns: list[str],
    rows: list[list[Any]],
    attempts: int,
    usage: Usage,
    narration_usage: Usage,
    answer: str | None,
    latency_ms: float,
    trace_id: str,
    model: str,
) -> AgentResult:
    # Cite only assets that were actually offered, intersect with what the model claims.
    cited = [a for a in gen.used_assets if a in context.offered_assets]
    return AgentResult(
        question=question,
        plan=gen.plan,
        sql=gen.sql,
        columns=rows_columns,
        rows=rows,
        used_assets=cited,
        selected_tables=context.selected_tables,
        selection_strategy=context.selection_strategy,
        selection_fell_back=context.selection_fell_back,
        selection_notes=context.notes,
        attempts=attempts,
        repaired=attempts > 1,
        usage=usage,
        narration_usage=narration_usage,
        answer=answer,
        latency_ms=latency_ms,
        trace_id=trace_id,
        model=model,
    )


def _narrate(
    question: str,
    columns: list[str],
    rows: list[list[Any]],
    *,
    llm: LLMClient,
    model: str,
    max_tokens: int,
    trace_writer: TraceWriter | None,
    trace_id: str,
    parent_span_id: str,
) -> tuple[str | None, Usage]:
    """Summarize executed ``rows`` into one grounded sentence; return ``(answer, usage)``.

    A single free-text call whose only input is the question plus a rendering of the
    result table (capped at :data:`_NARRATE_ROW_CAP`), so the sentence is anchored to real
    output and can't smuggle in numbers the SQL never produced. Traced as its own GenAI
    child span (invariant 7). An empty completion yields ``None`` rather than "" so callers
    can treat "no narration" uniformly."""
    request = LLMRequest(
        model=model,
        messages=[Message(role="user", content=_narration_prompt(question, columns, rows))],
        system=_NARRATE_SYSTEM,
        max_tokens=max_tokens,
        temperature=0.0,
    )
    response = llm.complete(request)
    if trace_writer is not None:
        trace_writer.write(
            llm_call_span(
                request,
                response,
                operation=_GEN_AI_OPERATION,
                name="narrate",
                trace_id=trace_id,
                parent_span_id=parent_span_id,
            )
        )
    text = (response.text or "").strip()
    return (text or None), response.usage


def _narration_prompt(question: str, columns: list[str], rows: list[list[Any]]) -> str:
    """Render the question + a compact pipe-delimited view of the (capped) result table."""
    shown = rows[:_NARRATE_ROW_CAP]
    lines = [" | ".join(columns)]
    lines += [" | ".join("" if v is None else str(v) for v in row) for row in shown]
    if len(rows) > len(shown):
        lines.append(f"… ({len(rows) - len(shown)} more rows not shown)")
    table = "\n".join(lines)
    return f"Question: {question}\n\nResult table ({len(rows)} row(s)):\n{table}"


def _repair_prompt(error: str) -> str:
    return (
        "That SQL failed with the following error:\n\n"
        f"{error}\n\n"
        "Return a corrected single read-only SELECT. Keep it grounded in the schema."
    )


def _reparse_prompt(error: str) -> str:
    """Repair prompt for a generation that didn't match the schema (vs. one whose SQL ran and
    errored). Names the miss so the model fills the required ``sql`` field on the next try."""
    return (
        "Your previous response could not be read as an answer:\n\n"
        f"{error}\n\n"
        "Reply with a JSON object holding `plan`, `sql`, and `used_assets`. The `sql` field is "
        "required and must contain a single read-only SELECT that answers the question — do not "
        "put the query only in `plan`."
    )


def _finish(writer: TraceWriter | None, span: Span, *, status: str) -> None:
    if writer is not None:
        writer.write(span.end(status="ok" if status == "ok" else "error"))


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)
