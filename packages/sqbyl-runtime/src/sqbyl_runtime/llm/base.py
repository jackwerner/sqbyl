"""The ``LLMClient`` seam (spec §9, §9.5).

One thin interface with three implementations (real / mock / record-replay) so
every LLM-touching code path is testable with zero network and CI never spends
tokens (invariant 4). Prompt-caching and strict-JSON structured output live
*inside* this seam, so callers never hand-roll either.

Usage/token accounting is baked into every response so the cost estimator and
spend meter (invariant 5) have a single source of truth.
"""

from __future__ import annotations

import abc
import hashlib
import json
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, Field

Role = Literal["user", "assistant"]
T = TypeVar("T", bound=BaseModel)


class RateLimitError(Exception):
    """A 429 / rate-limit signal from the provider (spec §3 #8).

    The seam raises this (the real client translates the provider's 429) so callers can
    tell "slow down, retry" apart from a genuine failure. The orchestrator retries on it
    with backoff instead of degrading the unit to a failed card; every *other* exception
    is a real failure. ``retry_after`` carries the provider's ``Retry-After`` hint (seconds)
    when present, so backoff can honor server guidance instead of guessing. ``usage`` carries
    any tokens the rejected call still billed (usually zero — a 429 rejects before
    completion) so the orchestrator can meter it rather than silently drop it (invariant 5).
    """

    def __init__(
        self,
        message: str = "",
        *,
        retry_after: float | None = None,
        usage: Usage | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.usage = usage


class Message(BaseModel):
    """A single conversation turn. ``system`` is passed separately to ``complete``."""

    role: Role
    content: str


class Usage(BaseModel):
    """Token accounting for one call, including prompt-cache hits/writes."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens
            + other.cache_creation_input_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
        )


class LLMResponse(BaseModel):
    """The result of one ``complete`` call.

    ``text`` is free-form output; ``structured`` is the parsed JSON object when a
    ``response_model`` was requested (the seam forces strict tool-use JSON).
    """

    model: str
    text: str | None = None
    structured: dict[str, Any] | None = None
    stop_reason: str | None = None
    usage: Usage = Field(default_factory=Usage)

    def parse(self, model_cls: type[T]) -> T:
        """Validate the structured payload into a pydantic model."""
        if self.structured is None:
            raise ValueError("response has no structured payload to parse")
        return model_cls.model_validate(self.structured)


class LLMRequest(BaseModel):
    """The full, hashable description of a call — the cassette key for record-replay."""

    model: str
    messages: list[Message]
    system: str | None = None
    response_schema: dict[str, Any] | None = None
    max_tokens: int = 4096
    temperature: float = 0.0
    cache_system: bool = False

    def fingerprint(self) -> str:
        """Stable sha256 over the canonical request — identical requests collide by design."""
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()


class LLMClient(abc.ABC):
    """Minimal completion interface. Implementations: real, mock, record-replay."""

    @abc.abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse:
        """Run one completion. Implementations own caching/structured-output details."""
        raise NotImplementedError

    def complete_text(
        self,
        messages: list[Message],
        *,
        model: str,
        system: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        cache_system: bool = False,
    ) -> LLMResponse:
        """Convenience: a free-text completion."""
        return self.complete(
            LLMRequest(
                model=model,
                messages=messages,
                system=system,
                max_tokens=max_tokens,
                temperature=temperature,
                cache_system=cache_system,
            )
        )

    def complete_structured(
        self,
        messages: list[Message],
        *,
        model: str,
        response_model: type[T],
        system: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        cache_system: bool = False,
    ) -> tuple[T, LLMResponse]:
        """Convenience: a strict-JSON completion validated into ``response_model``.

        Strict structured output is handled inside the seam so callers never
        hand-roll tool-use plumbing (spec §9.5).
        """
        response = self.complete(
            LLMRequest(
                model=model,
                messages=messages,
                system=system,
                response_schema=response_model.model_json_schema(),
                max_tokens=max_tokens,
                temperature=temperature,
                cache_system=cache_system,
            )
        )
        return response.parse(response_model), response
