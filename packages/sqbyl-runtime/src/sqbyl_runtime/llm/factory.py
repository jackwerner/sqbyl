"""Build a real :class:`LLMClient` for a named provider (spec §9).

A project picks *one* provider and uses it for everything — agent, judges, coach, synth
(no mixing). This factory is the single place that maps a provider name to its client, so
``runtime.load()`` and the dev toolkit share one dispatch and adding a provider is a one-line
change here. The SDK for each provider is an optional extra, imported lazily inside the
client, so this module is import-safe with neither installed.
"""

from __future__ import annotations

from collections.abc import Callable

from sqbyl_runtime.llm.anthropic_client import AnthropicLLMClient
from sqbyl_runtime.llm.base import LLMClient
from sqbyl_runtime.llm.openai_client import OpenAILLMClient

# Provider name -> constructor taking (api_key, base_url). Extend here to add a provider.
_PROVIDERS: dict[str, Callable[[str | None, str | None], LLMClient]] = {
    "anthropic": lambda api_key, base_url: AnthropicLLMClient(api_key=api_key, base_url=base_url),
    "openai": lambda api_key, base_url: OpenAILLMClient(api_key=api_key, base_url=base_url),
}

SUPPORTED_PROVIDERS = tuple(_PROVIDERS)


def build_provider_client(
    provider: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> LLMClient:
    """Construct the real client for ``provider`` (e.g. ``"anthropic"``, ``"openai"``)."""
    try:
        make = _PROVIDERS[provider]
    except KeyError:
        raise ValueError(
            f"unknown LLM provider {provider!r}; supported: {', '.join(SUPPORTED_PROVIDERS)}"
        ) from None
    return make(api_key, base_url)
