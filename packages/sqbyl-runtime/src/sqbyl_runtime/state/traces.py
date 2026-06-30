"""Local-first traces, shaped to OpenTelemetry GenAI semantic conventions (invariant 7).

Every trace sqbyl writes — from the very first — uses OTel GenAI attribute names so
``.sqbyl/traces/*.jsonl`` is exportable to any OTel backend without reshaping. This
module owns the span model, the GenAI attribute keys, a builder from the LLM seam's
request/response, and a JSONL writer/reader.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from sqbyl_runtime.llm.base import LLMRequest, LLMResponse

# --- OTel GenAI semantic-convention attribute keys ------------------------------
GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"
GEN_AI_REQUEST_TEMPERATURE = "gen_ai.request.temperature"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
# Cache token counts aren't in the stable spec yet; namespaced under sqbyl.* so a
# generic OTel consumer ignores them while sqbyl's own tooling can read them.
SQBYL_USAGE_CACHE_CREATION_TOKENS = "sqbyl.gen_ai.usage.cache_creation_input_tokens"
SQBYL_USAGE_CACHE_READ_TOKENS = "sqbyl.gen_ai.usage.cache_read_input_tokens"

SpanStatus = Literal["unset", "ok", "error"]


def new_trace_id() -> str:
    """A 128-bit trace id as 32 hex chars (OTel format)."""
    return secrets.token_hex(16)


def new_span_id() -> str:
    """A 64-bit span id as 16 hex chars (OTel format)."""
    return secrets.token_hex(8)


class Span(BaseModel):
    """An OTel-shaped span. Local representation; maps 1:1 to an OTLP span."""

    name: str
    trace_id: str = Field(default_factory=new_trace_id)
    span_id: str = Field(default_factory=new_span_id)
    parent_span_id: str | None = None
    start_time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    end_time: datetime | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    status: SpanStatus = "unset"
    events: list[dict[str, Any]] = Field(default_factory=list)

    def end(self, *, status: SpanStatus = "ok") -> Span:
        """Stamp the end time and status. Returns self."""
        self.end_time = datetime.now(UTC)
        self.status = status
        return self


def llm_call_span(
    request: LLMRequest,
    response: LLMResponse,
    *,
    system: str = "anthropic",
    operation: str = "chat",
    name: str | None = None,
    trace_id: str | None = None,
    parent_span_id: str | None = None,
) -> Span:
    """Build a finished GenAI span describing one LLM call."""
    attrs: dict[str, Any] = {
        GEN_AI_SYSTEM: system,
        GEN_AI_OPERATION_NAME: operation,
        GEN_AI_REQUEST_MODEL: request.model,
        GEN_AI_REQUEST_MAX_TOKENS: request.max_tokens,
        GEN_AI_REQUEST_TEMPERATURE: request.temperature,
        GEN_AI_RESPONSE_MODEL: response.model,
        GEN_AI_USAGE_INPUT_TOKENS: response.usage.input_tokens,
        GEN_AI_USAGE_OUTPUT_TOKENS: response.usage.output_tokens,
        SQBYL_USAGE_CACHE_CREATION_TOKENS: response.usage.cache_creation_input_tokens,
        SQBYL_USAGE_CACHE_READ_TOKENS: response.usage.cache_read_input_tokens,
    }
    if response.stop_reason is not None:
        attrs[GEN_AI_RESPONSE_FINISH_REASONS] = [response.stop_reason]
    span = Span(
        name=name or f"{operation} {request.model}",
        trace_id=trace_id or new_trace_id(),
        parent_span_id=parent_span_id,
        attributes=attrs,
    )
    return span.end(status="ok")


class TraceWriter:
    """Append spans as JSON lines to a trace file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, span: Span) -> None:
        with self.path.open("a") as fh:
            fh.write(span.model_dump_json() + "\n")


def read_spans(path: str | Path) -> list[Span]:
    """Read every span back from a JSONL trace file."""
    p = Path(path)
    if not p.exists():
        return []
    return [Span.model_validate_json(line) for line in p.read_text().splitlines() if line.strip()]
