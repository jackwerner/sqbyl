"""The stateless agent pipeline — ``ask()`` (spec §5 steps 3-7, plan 2.2).

One ``ask()`` is a stateless pipeline: **generate** (a short plan + candidate SQL,
via strict structured output) → **static-validate** (``EXPLAIN``, no execution) →
**execute** (read-only) → **self-repair** (feed the error back, up to N times) →
**respond** ``{plan, sql, rows, used_assets, usage, latency}``.

This lives in ``sqbyl-runtime`` so the shipped "model with logs" is correct from day
one: the runtime owns generation, validation, read-only execution, repair, and the
OTel-shaped trace every run writes. Everything that *improves* the agent (eval,
coach, …) lives in ``sqbyl`` and is built on top of this.

Multi-turn is just a thread of these with prior turns prepended — not modeled here.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

from sqbyl_runtime.context import CompiledContext, ProjectKnowledge
from sqbyl_runtime.db import Database, QueryResult, StaticValidationError, WriteAttemptError
from sqbyl_runtime.llm.base import LLMClient, LLMRequest, Message, Usage
from sqbyl_runtime.state.traces import Span, TraceWriter, llm_call_span, new_span_id, new_trace_id

# OTel GenAI operation name for an agent invocation.
_GEN_AI_OPERATION = "chat"


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
    attempts: int = 0
    repaired: bool = False
    error: str | None = None
    usage: Usage = Field(default_factory=Usage)
    latency_ms: float = 0.0
    trace_id: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None


def ask(
    question: str,
    *,
    knowledge: ProjectKnowledge,
    db: Database,
    llm: LLMClient,
    model: str,
    self_repair_attempts: int = 2,
    max_tokens: int = 2048,
    trace_writer: TraceWriter | None = None,
) -> AgentResult:
    """Answer ``question`` against ``db`` using ``knowledge`` and ``llm``.

    Generates, statically validates, executes read-only, and self-repairs up to
    ``self_repair_attempts`` times. Always writes an OTel-shaped run span (plus a
    child span per LLM call) when a ``trace_writer`` is given.
    """
    started = time.perf_counter()
    context = knowledge.compile(question)
    trace_id = new_trace_id()
    run_span = Span(
        name="ask",
        trace_id=trace_id,
        span_id=new_span_id(),
        attributes={"gen_ai.operation.name": _GEN_AI_OPERATION, "sqbyl.question": question},
    )

    messages: list[Message] = [Message(role="user", content=context.user)]
    total_usage = Usage()
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
        gen = response.parse(AgentGeneration)
        last_gen = gen

        rows, error = _validate_and_execute(db, gen.sql)
        if error is None:
            assert rows is not None
            result = _success(
                question,
                gen,
                context,
                rows_columns=rows.columns,
                rows=[list(r) for r in rows.rows],
                attempts=attempt_index + 1,
                usage=total_usage,
                latency_ms=_elapsed_ms(started),
                trace_id=trace_id,
            )
            _finish(trace_writer, run_span, status="ok")
            return result

        attempts.append(AgentAttempt(sql=gen.sql, plan=gen.plan, error=error))
        # Thread the failure back in for the next attempt (keeps role alternation).
        messages.append(Message(role="assistant", content=gen.sql))
        messages.append(Message(role="user", content=_repair_prompt(error)))

    # Exhausted all repair attempts.
    assert last_gen is not None
    _finish(trace_writer, run_span, status="error")
    return AgentResult(
        question=question,
        plan=last_gen.plan,
        sql=last_gen.sql,
        used_assets=last_gen.used_assets,
        selected_tables=context.selected_tables,
        attempts=len(attempts),
        repaired=len(attempts) > 1,
        error=attempts[-1].error,
        usage=total_usage,
        latency_ms=_elapsed_ms(started),
        trace_id=trace_id,
    )


def _validate_and_execute(db: Database, sql: str) -> tuple[QueryResult | None, str | None]:
    """Static-validate (``EXPLAIN``) then execute; return ``(rows, None)`` on success
    or ``(None, error)`` so the caller can self-repair without re-running the query."""
    try:
        db.explain(sql)
    except (StaticValidationError, WriteAttemptError) as exc:
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
    latency_ms: float,
    trace_id: str,
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
        attempts=attempts,
        repaired=attempts > 1,
        usage=usage,
        latency_ms=latency_ms,
        trace_id=trace_id,
    )


def _repair_prompt(error: str) -> str:
    return (
        "That SQL failed with the following error:\n\n"
        f"{error}\n\n"
        "Return a corrected single read-only SELECT. Keep it grounded in the schema."
    )


def _finish(writer: TraceWriter | None, span: Span, *, status: str) -> None:
    if writer is not None:
        writer.write(span.end(status="ok" if status == "ok" else "error"))


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)
