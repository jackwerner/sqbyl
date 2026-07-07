"""``OpenAILLMClient`` — the real OpenAI-backed implementation of the seam (spec §9).

A structural twin of :class:`~sqbyl_runtime.llm.anthropic_client.AnthropicLLMClient`:
same lazy-import + double-checked-lock construction, same ``base_url`` passthrough, same
429 → :class:`RateLimitError` translation. Everything provider-specific is handled *inside*
the seam so callers never see it:

- **System prompt** is sent as a leading ``system`` message (OpenAI has no separate param).
- **Strict-JSON structured output** uses a single forced function call whose ``parameters``
  is the requested pydantic schema — the exact mirror of the Anthropic forced-tool trick.
  We validate the arguments with pydantic on our side (``LLMResponse.parse``), so arbitrary
  schemas work without OpenAI strict-mode constraints.
- **Prompt caching** is automatic on OpenAI, so ``cache_system`` is a no-op here; cached
  input tokens are still read back into :class:`Usage` for accurate cost metering.
- **Reasoning/newer models** (e.g. ``gpt-5``) reject a custom ``temperature`` and the older
  ``max_tokens`` param. Rather than hardcode a model table, a small compatibility shim strips
  an offending parameter and retries when the API reports it as unsupported.

The ``openai`` SDK is imported lazily so merely importing this module (e.g. in CI, which has
no key and may not install the extra) costs nothing and never reaches for an ambient key.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

from sqbyl_runtime.llm._errors import is_rate_limit, retry_after
from sqbyl_runtime.llm.base import LLMClient, LLMRequest, LLMResponse, RateLimitError, Usage

# Name of the synthetic function used to coerce strict-JSON structured output.
_STRUCTURED_TOOL = "emit_result"

# Params the compatibility shim knows how to drop/rewrite when a model rejects them.
_TEMPERATURE = "temperature"
_MAX_COMPLETION = "max_completion_tokens"
_MAX_TOKENS = "max_tokens"


class OpenAILLMClient(LLMClient):
    def __init__(
        self,
        api_key: str | None = None,
        client: Any | None = None,
        base_url: str | None = None,
    ) -> None:
        # Allow injecting a pre-built SDK client (used by tests); otherwise build lazily.
        # ``base_url`` points the SDK at an alternate OpenAI-compatible endpoint — a corporate
        # proxy, an AI gateway (LiteLLM, Cloudflare), or Azure/OpenAI-compatible relay — without
        # changing anything else. It's a plain URL, not a secret.
        self._client = client
        self._api_key = api_key
        self._base_url = base_url
        self._lock = threading.Lock()

    def _ensure_client(self) -> Any:
        # Double-checked lock: concurrent first calls (a threadpool serving an async API)
        # must not each build a separate SDK client. The SDK client is thread-safe once built.
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            key = self._api_key or os.environ.get("OPENAI_API_KEY")
            if not key:
                raise RuntimeError(
                    "OPENAI_API_KEY is not set; the real client needs a key "
                    "(use MockLLMClient or RecordReplayLLMClient in tests)"
                )
            try:
                import openai
            except ImportError as exc:  # pragma: no cover - import guard
                raise RuntimeError(
                    "the 'openai' package is required for the OpenAI client "
                    "(install the extra: pip install 'sqbyl-runtime[openai]')"
                ) from exc
            kwargs: dict[str, Any] = {"api_key": key}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = openai.OpenAI(**kwargs)
        return self._client

    def _messages(self, request: LLMRequest) -> list[dict[str, str]]:
        # OpenAI carries the system prompt as a leading message, not a separate param.
        # ``cache_system`` is intentionally ignored: OpenAI prompt-caches automatically.
        messages: list[dict[str, str]] = []
        if request.system is not None:
            messages.append({"role": "system", "content": request.system})
        messages.extend(m.model_dump() for m in request.messages)
        return messages

    def check_auth(self) -> None:
        """Confirm the key works via a **token-free** models-list call (finding #5).

        Raises ``RuntimeError`` with an actionable message on a missing/invalid key, so
        ``init`` can fail fast before quoting a plan the user can't run."""
        client = self._ensure_client()
        try:
            client.models.list()
        except Exception as exc:  # SDK auth/connection errors → one friendly message
            raise RuntimeError(
                "OpenAI credential check failed — verify OPENAI_API_KEY (or the api_key in "
                f"sqbyl.yaml) and network access. Underlying error: {exc}"
            ) from exc

    def complete(self, request: LLMRequest) -> LLMResponse:
        client = self._ensure_client()
        kwargs: dict[str, Any] = {
            "model": request.model,
            "max_completion_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": self._messages(request),
        }

        structured = request.response_schema is not None
        if structured:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": _STRUCTURED_TOOL,
                        "description": "Emit the result as a structured object.",
                        "parameters": request.response_schema,
                    },
                }
            ]
            kwargs["tool_choice"] = {
                "type": "function",
                "function": {"name": _STRUCTURED_TOOL},
            }

        completion = self._create_with_compat(client, kwargs)
        return _to_response(completion, structured=structured)

    def _create_with_compat(self, client: Any, kwargs: dict[str, Any]) -> Any:
        """Call the chat API, retrying with an offending param stripped if the model rejects it.

        Newer models fix ``temperature`` and use ``max_completion_tokens``; some endpoints still
        want the legacy ``max_tokens``. We try, and on a "this parameter isn't supported" 400 we
        adjust one param and retry — bounded, so a genuinely broken request still surfaces.
        """
        attempt = dict(kwargs)
        for _ in range(3):
            try:
                return client.chat.completions.create(**attempt)
            except Exception as exc:  # noqa: BLE001 - re-raised below unless we can adapt
                if is_rate_limit(exc):
                    raise RateLimitError(str(exc), retry_after=retry_after(exc)) from exc
                adjusted = _adjust_for_unsupported_param(exc, attempt)
                if adjusted is None:
                    raise
                attempt = adjusted
        # Exhausted adjustments — one final attempt so any error propagates verbatim.
        return client.chat.completions.create(**attempt)


def _adjust_for_unsupported_param(exc: Exception, kwargs: dict[str, Any]) -> dict[str, Any] | None:
    """Return a copy of ``kwargs`` with one unsupported param fixed, or ``None`` if we can't help.

    Matched on the error message (the SDK surfaces the offending field name in the 400 body).
    """
    message = str(exc).lower()
    adjusted = dict(kwargs)
    # A model that pins temperature (e.g. reasoning models) rejects a non-default value.
    if _TEMPERATURE in message and _TEMPERATURE in adjusted:
        adjusted.pop(_TEMPERATURE)
        return adjusted
    # An endpoint that wants the legacy token cap: rename max_completion_tokens -> max_tokens.
    if _MAX_COMPLETION in message and _MAX_COMPLETION in adjusted:
        adjusted[_MAX_TOKENS] = adjusted.pop(_MAX_COMPLETION)
        return adjusted
    return None


def _to_response(completion: Any, *, structured: bool) -> LLMResponse:
    """Translate an OpenAI chat completion into our flat ``LLMResponse``."""
    choice = completion.choices[0]
    message = choice.message

    text: str | None = getattr(message, "content", None) or None
    payload: dict[str, Any] | None = None
    tool_calls = getattr(message, "tool_calls", None)
    if structured and tool_calls:
        raw = tool_calls[0].function.arguments
        payload = raw if isinstance(raw, dict) else json.loads(raw)

    usage = _usage_from(getattr(completion, "usage", None))
    return LLMResponse(
        model=getattr(completion, "model", ""),
        text=None if structured else text,
        structured=payload if structured else None,
        stop_reason=getattr(choice, "finish_reason", None),
        usage=usage,
    )


def _usage_from(raw_usage: Any) -> Usage:
    """Map OpenAI token accounting into :class:`Usage`, splitting out cached input tokens.

    OpenAI reports total ``prompt_tokens`` (which *include* any cached read) plus a
    ``prompt_tokens_details.cached_tokens`` breakout. We record the cached portion under
    ``cache_read_input_tokens`` and the remainder as fresh ``input_tokens`` so the cost meter
    prices the cache discount correctly. OpenAI has no separate cache-*write* charge.
    """
    if raw_usage is None:
        return Usage()
    prompt = getattr(raw_usage, "prompt_tokens", 0) or 0
    completion = getattr(raw_usage, "completion_tokens", 0) or 0
    details = getattr(raw_usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) or 0 if details is not None else 0
    return Usage(
        input_tokens=max(prompt - cached, 0),
        output_tokens=completion,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=cached,
    )
