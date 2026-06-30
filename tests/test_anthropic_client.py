"""Phase 0.3 — the real Anthropic client's request-build + response-parse logic.

Exercised with an injected fake SDK client so there is zero network and no key
(invariant 4). We assert the seam builds the structured-output tool and the
prompt-cache control, and flattens the SDK message + usage correctly.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from sqbyl_runtime.llm import LLMRequest, Message
from sqbyl_runtime.llm.anthropic_client import AnthropicLLMClient


class FakeMessages:
    def __init__(self, reply: Any) -> None:
        self._reply = reply
        self.last_kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        return self._reply


class FakeSDK:
    def __init__(self, reply: Any) -> None:
        self.messages = FakeMessages(reply)


def _text_message() -> Any:
    return SimpleNamespace(
        model="claude-opus-4-8",
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="hello")],
        usage=SimpleNamespace(
            input_tokens=11,
            output_tokens=4,
            cache_creation_input_tokens=20,
            cache_read_input_tokens=0,
        ),
    )


def _tool_message() -> Any:
    return SimpleNamespace(
        model="claude-opus-4-8",
        stop_reason="tool_use",
        content=[SimpleNamespace(type="tool_use", input={"plan": "p", "sql": "SELECT 1"})],
        usage=SimpleNamespace(input_tokens=5, output_tokens=2),
    )


def test_text_completion_flattens_message_and_usage() -> None:
    sdk = FakeSDK(_text_message())
    client = AnthropicLLMClient(client=sdk)
    resp = client.complete(
        LLMRequest(model="claude-opus-4-8", messages=[Message(role="user", content="hi")])
    )
    assert resp.text == "hello"
    assert resp.usage.input_tokens == 11
    assert resp.usage.cache_creation_input_tokens == 20
    assert resp.structured is None


def test_prompt_caching_tags_system_block() -> None:
    sdk = FakeSDK(_text_message())
    client = AnthropicLLMClient(client=sdk)
    client.complete(
        LLMRequest(
            model="claude-opus-4-8",
            messages=[Message(role="user", content="hi")],
            system="stable schema block",
            cache_system=True,
        )
    )
    system = sdk.messages.last_kwargs["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert system[0]["text"] == "stable schema block"


def test_uncached_system_is_a_plain_string() -> None:
    sdk = FakeSDK(_text_message())
    client = AnthropicLLMClient(client=sdk)
    client.complete(
        LLMRequest(
            model="claude-opus-4-8",
            messages=[Message(role="user", content="hi")],
            system="block",
        )
    )
    assert sdk.messages.last_kwargs["system"] == "block"


def test_structured_output_forces_the_tool() -> None:
    sdk = FakeSDK(_tool_message())
    client = AnthropicLLMClient(client=sdk)
    resp = client.complete(
        LLMRequest(
            model="claude-opus-4-8",
            messages=[Message(role="user", content="q")],
            response_schema={"type": "object", "properties": {"sql": {"type": "string"}}},
        )
    )
    kwargs = sdk.messages.last_kwargs
    assert kwargs["tool_choice"] == {"type": "tool", "name": "emit_result"}
    assert kwargs["tools"][0]["name"] == "emit_result"
    assert resp.structured == {"plan": "p", "sql": "SELECT 1"}


def test_missing_key_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = AnthropicLLMClient()
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        client.complete(
            LLMRequest(model="claude-opus-4-8", messages=[Message(role="user", content="hi")])
        )
