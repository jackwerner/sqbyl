"""The LLMClient seam: one interface, three implementations (spec §9, §9.5)."""

from __future__ import annotations

from sqbyl_runtime.llm.anthropic_client import AnthropicLLMClient
from sqbyl_runtime.llm.base import (
    LLMClient,
    LLMRequest,
    LLMResponse,
    Message,
    Usage,
)
from sqbyl_runtime.llm.mock import (
    MockLLMClient,
    structured_reply,
    text_reply,
)
from sqbyl_runtime.llm.replay import (
    CassetteMissError,
    RecordReplayLLMClient,
)

__all__ = [
    "AnthropicLLMClient",
    "CassetteMissError",
    "LLMClient",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "MockLLMClient",
    "RecordReplayLLMClient",
    "Usage",
    "structured_reply",
    "text_reply",
]
