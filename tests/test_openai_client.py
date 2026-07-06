"""The real OpenAI client's request-build + response-parse logic (spec §9).

Exercised with an injected fake SDK client so there is zero network and no key
(invariant 4). We assert the seam builds the forced-function structured output, maps
OpenAI's usage (incl. cached tokens) into our ``Usage``, translates a 429, and — the
provider-specific wrinkle — strips an unsupported ``temperature`` and retries.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from sqbyl_runtime.llm import LLMRequest, Message
from sqbyl_runtime.llm.base import RateLimitError
from sqbyl_runtime.llm.openai_client import OpenAILLMClient


class FakeCompletions:
    def __init__(self, reply: Any) -> None:
        # ``reply`` is a completion, an exception, or a list of per-call outcomes (raise/return).
        self._reply = reply
        self._script = list(reply) if isinstance(reply, list) else None
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self._script.pop(0) if self._script is not None else self._reply
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    @property
    def last_kwargs(self) -> dict[str, Any]:
        return self.calls[-1]


class FakeSDK:
    def __init__(self, reply: Any) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions(reply))


def _sdk_429(*, retry_after: str | None = "3") -> Exception:
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    exc = type("RateLimitError", (Exception,), {})("rate limited")
    exc.status_code = 429  # type: ignore[attr-defined]
    exc.response = SimpleNamespace(headers=headers)  # type: ignore[attr-defined]
    return exc


def _bad_request(param: str) -> Exception:
    exc = type("BadRequestError", (Exception,), {})(
        f"Unsupported value: '{param}' is not supported with this model."
    )
    exc.status_code = 400  # type: ignore[attr-defined]
    return exc


def _text_completion() -> Any:
    return SimpleNamespace(
        model="gpt-5",
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="hello", tool_calls=None),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=30,
            completion_tokens=4,
            prompt_tokens_details=SimpleNamespace(cached_tokens=10),
        ),
    )


def _tool_completion() -> Any:
    tool_call = SimpleNamespace(
        function=SimpleNamespace(name="emit_result", arguments='{"plan": "p", "sql": "SELECT 1"}')
    )
    return SimpleNamespace(
        model="gpt-5",
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(content=None, tool_calls=[tool_call]),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=5,
            completion_tokens=2,
            prompt_tokens_details=None,
        ),
    )


def test_text_completion_flattens_message_and_maps_cached_usage() -> None:
    sdk = FakeSDK(_text_completion())
    client = OpenAILLMClient(client=sdk)
    resp = client.complete(LLMRequest(model="gpt-5", messages=[Message(role="user", content="hi")]))
    assert resp.text == "hello"
    assert resp.structured is None
    # prompt_tokens (30) split into fresh input (20) + cached read (10); completion -> output.
    assert resp.usage.input_tokens == 20
    assert resp.usage.cache_read_input_tokens == 10
    assert resp.usage.output_tokens == 4
    assert resp.usage.cache_creation_input_tokens == 0


def test_system_prompt_becomes_a_leading_message() -> None:
    sdk = FakeSDK(_text_completion())
    client = OpenAILLMClient(client=sdk)
    client.complete(
        LLMRequest(
            model="gpt-5",
            messages=[Message(role="user", content="hi")],
            system="stable schema block",
            cache_system=True,  # a no-op for OpenAI — no crash, no special handling
        )
    )
    messages = sdk.chat.completions.last_kwargs["messages"]
    assert messages[0] == {"role": "system", "content": "stable schema block"}
    assert messages[1]["role"] == "user"


def test_structured_output_forces_the_function() -> None:
    sdk = FakeSDK(_tool_completion())
    client = OpenAILLMClient(client=sdk)
    resp = client.complete(
        LLMRequest(
            model="gpt-5",
            messages=[Message(role="user", content="q")],
            response_schema={"type": "object", "properties": {"sql": {"type": "string"}}},
        )
    )
    kwargs = sdk.chat.completions.last_kwargs
    assert kwargs["tool_choice"] == {"type": "function", "function": {"name": "emit_result"}}
    assert kwargs["tools"][0]["function"]["name"] == "emit_result"
    assert resp.structured == {"plan": "p", "sql": "SELECT 1"}
    assert resp.text is None


def test_unsupported_temperature_is_stripped_and_retried() -> None:
    # First call 400s on temperature; the shim drops it and the retry succeeds.
    sdk = FakeSDK([_bad_request("temperature"), _text_completion()])
    client = OpenAILLMClient(client=sdk)
    resp = client.complete(LLMRequest(model="gpt-5", messages=[Message(role="user", content="hi")]))
    assert resp.text == "hello"
    assert len(sdk.chat.completions.calls) == 2
    assert "temperature" in sdk.chat.completions.calls[0]
    assert "temperature" not in sdk.chat.completions.calls[1]


def test_legacy_max_tokens_param_is_renamed_and_retried() -> None:
    sdk = FakeSDK([_bad_request("max_completion_tokens"), _text_completion()])
    client = OpenAILLMClient(client=sdk)
    client.complete(LLMRequest(model="gpt-5", messages=[Message(role="user", content="hi")]))
    first, second = sdk.chat.completions.calls
    assert "max_completion_tokens" in first and "max_tokens" not in first
    assert "max_tokens" in second and "max_completion_tokens" not in second


def test_429_is_translated_to_ratelimiterror_with_retry_after() -> None:
    sdk = FakeSDK(_sdk_429(retry_after="3"))
    client = OpenAILLMClient(client=sdk)
    with pytest.raises(RateLimitError) as exc_info:
        client.complete(LLMRequest(model="gpt-5", messages=[Message(role="user", content="hi")]))
    assert exc_info.value.retry_after == 3.0


def test_non_adaptable_error_propagates_unchanged() -> None:
    boom = ValueError("bad request")
    client = OpenAILLMClient(client=FakeSDK(boom))
    with pytest.raises(ValueError, match="bad request"):
        client.complete(LLMRequest(model="gpt-5", messages=[Message(role="user", content="hi")]))


def test_missing_key_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = OpenAILLMClient()
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        client.complete(LLMRequest(model="gpt-5", messages=[Message(role="user", content="hi")]))
