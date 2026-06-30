"""``MockLLMClient`` — scripted, deterministic responses for unit tests (spec §9.5).

No network, no key. Every LLM-touching code path ships with mock-based tests built
on this (invariant 4).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqbyl_runtime.llm.base import LLMClient, LLMRequest, LLMResponse, Usage

# A scripted reply is either a ready-made response or a function of the request
# (so a test can, e.g., return bad SQL on the first call and good SQL on the next).
ScriptedReply = LLMResponse | Callable[[LLMRequest], LLMResponse]

_DEFAULT_USAGE = Usage(input_tokens=10, output_tokens=5)


def text_reply(text: str, *, model: str = "mock", usage: Usage | None = None) -> LLMResponse:
    """Build a free-text scripted reply."""
    return LLMResponse(
        model=model, text=text, usage=usage or _DEFAULT_USAGE, stop_reason="end_turn"
    )


def structured_reply(
    payload: dict[str, Any], *, model: str = "mock", usage: Usage | None = None
) -> LLMResponse:
    """Build a structured (strict-JSON) scripted reply."""
    return LLMResponse(
        model=model,
        structured=payload,
        usage=usage or _DEFAULT_USAGE,
        stop_reason="tool_use",
    )


class MockLLMClient(LLMClient):
    """Returns scripted replies in order; records every request it received."""

    def __init__(self, replies: list[ScriptedReply] | None = None) -> None:
        self._replies: list[ScriptedReply] = list(replies or [])
        self.requests: list[LLMRequest] = []
        self._cursor = 0

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def queue(self, reply: ScriptedReply) -> None:
        """Append another scripted reply."""
        self._replies.append(reply)

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self._cursor >= len(self._replies):
            raise AssertionError(
                f"MockLLMClient exhausted: no scripted reply for call #{self._cursor + 1} "
                f"(model={request.model!r})"
            )
        reply = self._replies[self._cursor]
        self._cursor += 1
        resolved = reply(request) if callable(reply) else reply
        # If the script didn't pin a model, echo the requested one for realism.
        if resolved.model == "mock":
            resolved = resolved.model_copy(update={"model": request.model})
        return resolved
