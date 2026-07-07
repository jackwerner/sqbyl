"""`base_url` passthrough — route the Claude client through an alternate endpoint (§2.2).

A corporate proxy or an AI gateway (LiteLLM, Cloudflare) is reachable by pointing the
Anthropic SDK at a different base URL; everything else is unchanged. Configurable in the
manifest (`model.base_url`, plain or `env:`) and on the runtime `load()`.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from sqbyl.llm import build_llm_client
from sqbyl.models import DatabaseConfig, ModelConfig, SqbylManifest
from sqbyl_runtime.llm.anthropic_client import AnthropicLLMClient
from sqbyl_runtime.models import Dialect


def _manifest(base_url: str | None = None) -> SqbylManifest:
    return SqbylManifest(
        name="t",
        database=DatabaseConfig(dialect=Dialect.duckdb, url="env:DATABASE_URL"),
        model=ModelConfig(api_key="sk-test", base_url=base_url),
    )


def test_client_passes_base_url_to_the_sdk() -> None:
    client = AnthropicLLMClient(api_key="sk-test", base_url="https://gateway.example/v1")
    sdk = client._ensure_client()  # constructing the SDK does not open a connection
    # Assert the exact host, not a substring/prefix (an "https://gateway.example" prefix
    # check would also pass "https://gateway.example.evil.com").
    assert urlsplit(str(sdk.base_url)).hostname == "gateway.example"


def test_client_without_base_url_uses_the_anthropic_default() -> None:
    sdk = AnthropicLLMClient(api_key="sk-test")._ensure_client()
    # Exact host match — an `"anthropic.com" in url` check would also accept a hostile
    # `https://anthropic.com.evil.com`.
    assert urlsplit(str(sdk.base_url)).hostname == "api.anthropic.com"


def test_build_llm_client_threads_a_plain_base_url() -> None:
    client = build_llm_client(_manifest(base_url="https://gw.internal/v1"))
    assert isinstance(client, AnthropicLLMClient)
    assert client._base_url == "https://gw.internal/v1"


def test_build_llm_client_resolves_an_env_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SQBYL_GATEWAY", "https://gw.env/v1")
    client = build_llm_client(_manifest(base_url="env:SQBYL_GATEWAY"))
    assert isinstance(client, AnthropicLLMClient)
    assert client._base_url == "https://gw.env/v1"


def test_no_base_url_by_default() -> None:
    client = build_llm_client(_manifest())
    assert isinstance(client, AnthropicLLMClient)
    assert client._base_url is None
