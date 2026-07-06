"""The provider factory (spec §9): one name -> one real client, no mixing.

A project picks a single provider; this is the one dispatch that turns that choice into a
client for both ``runtime.load()`` and the dev toolkit. Construction is lazy (the SDK is only
imported when the client first makes a call), so building a client here spends nothing.
"""

from __future__ import annotations

import pytest

from sqbyl_runtime.llm.anthropic_client import AnthropicLLMClient
from sqbyl_runtime.llm.factory import SUPPORTED_PROVIDERS, build_provider_client
from sqbyl_runtime.llm.openai_client import OpenAILLMClient


def test_supported_providers() -> None:
    assert SUPPORTED_PROVIDERS == ("anthropic", "openai")


@pytest.mark.parametrize(
    ("provider", "expected"),
    [("anthropic", AnthropicLLMClient), ("openai", OpenAILLMClient)],
)
def test_builds_the_right_client(provider: str, expected: type) -> None:
    client = build_provider_client(provider, api_key="k", base_url="https://example.test")
    assert isinstance(client, expected)


def test_unknown_provider_is_a_clear_error() -> None:
    with pytest.raises(ValueError, match="unknown LLM provider 'gemini'"):
        build_provider_client("gemini", api_key="k")
