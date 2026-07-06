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
from sqbyl_runtime.llm.factory import (
    SUPPORTED_PROVIDERS,
    build_provider_client,
)
from sqbyl_runtime.llm.mock import (
    MockLLMClient,
    structured_reply,
    text_reply,
)
from sqbyl_runtime.llm.openai_client import OpenAILLMClient
from sqbyl_runtime.llm.replay import (
    CassetteMissError,
    RecordReplayLLMClient,
)

__all__ = [
    "SUPPORTED_PROVIDERS",
    "AnthropicLLMClient",
    "CassetteMissError",
    "LLMClient",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "MockLLMClient",
    "OpenAILLMClient",
    "RecordReplayLLMClient",
    "Usage",
    "build_provider_client",
    "structured_reply",
    "text_reply",
]
