"""``AnthropicLLMClient`` — the real Claude-backed implementation (spec §9).

Handles, inside the seam so callers don't:
- **strict-JSON structured output** via a single forced tool whose ``input_schema``
  is the requested pydantic schema;
- **prompt caching** by tagging the (stable) system block with ``cache_control``;
- **usage accounting**, including cache read/write tokens.

The ``anthropic`` SDK is imported lazily so that merely importing this module (e.g.
in CI, which has no key) costs nothing and never reaches for an ambient key.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

from sqbyl_runtime.llm._errors import is_rate_limit, retry_after
from sqbyl_runtime.llm.base import LLMClient, LLMRequest, LLMResponse, RateLimitError, Usage

# Name of the synthetic tool used to coerce strict-JSON structured output.
_STRUCTURED_TOOL = "emit_result"

# Param the compatibility shim knows how to drop when a model rejects it.
_TEMPERATURE = "temperature"


class AnthropicLLMClient(LLMClient):
    def __init__(
        self,
        api_key: str | None = None,
        client: Any | None = None,
        base_url: str | None = None,
    ) -> None:
        # Allow injecting a pre-built SDK client (used by tests); otherwise build lazily.
        # ``base_url`` points the SDK at an alternate Claude endpoint — a corporate proxy,
        # an AI gateway (LiteLLM, Cloudflare), or a self-hosted relay — without changing
        # anything else. It's a plain URL, not a secret.
        self._client = client
        self._api_key = api_key
        self._base_url = base_url
        self._lock = threading.Lock()

    def _ensure_client(self) -> Any:
        # Double-checked lock: concurrent first calls (a threadpool serving an async API)
        # must not each build a separate SDK client. The SDK client itself is thread-safe
        # for concurrent requests once built.
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            key = self._api_key or os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise RuntimeError(
                    "ANTHROPIC_API_KEY is not set; the real client needs a key "
                    "(use MockLLMClient or RecordReplayLLMClient in tests)"
                )
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - import guard
                raise RuntimeError(
                    "the 'anthropic' package is required for the real client"
                ) from exc
            kwargs: dict[str, Any] = {"api_key": key}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

    def _system_param(self, request: LLMRequest) -> Any:
        if request.system is None:
            return None
        if not request.cache_system:
            return request.system
        # Cache the stable system block so repeated calls reuse it (spec §9, invariant on caching).
        return [
            {
                "type": "text",
                "text": request.system,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def complete(self, request: LLMRequest) -> LLMResponse:
        client = self._ensure_client()
        kwargs: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": [m.model_dump() for m in request.messages],
        }
        system = self._system_param(request)
        if system is not None:
            kwargs["system"] = system

        structured = request.response_schema is not None
        if structured:
            kwargs["tools"] = [
                {
                    "name": _STRUCTURED_TOOL,
                    "description": "Emit the result as a structured object.",
                    "input_schema": request.response_schema,
                }
            ]
            kwargs["tool_choice"] = {"type": "tool", "name": _STRUCTURED_TOOL}

        message = self._create_with_compat(client, kwargs)
        return _to_response(message, structured=structured)

    def _create_with_compat(self, client: Any, kwargs: dict[str, Any]) -> Any:
        """Call the messages API, retrying with an offending param stripped if the model rejects it.

        Newer Claude models (e.g. ``claude-opus-4-8``) deprecate a custom ``temperature`` and
        return a 400 naming it. Rather than hardcode a per-model table, we try, and on such a
        "this parameter isn't supported" 400 we drop the param and retry — bounded, so a
        genuinely broken request still surfaces. (Mirrors the OpenAI client's shim.)
        """
        attempt = dict(kwargs)
        for _ in range(2):
            try:
                return client.messages.create(**attempt)
            except Exception as exc:  # noqa: BLE001 - re-raised below unless we can adapt
                if is_rate_limit(exc):
                    raise RateLimitError(str(exc), retry_after=retry_after(exc)) from exc
                adjusted = _adjust_for_unsupported_param(exc, attempt)
                if adjusted is None:
                    raise
                attempt = adjusted
        # Exhausted adjustments — one final attempt so any error propagates verbatim.
        return client.messages.create(**attempt)


def _adjust_for_unsupported_param(exc: Exception, kwargs: dict[str, Any]) -> dict[str, Any] | None:
    """Return a copy of ``kwargs`` with one unsupported param dropped, or ``None`` if we can't help.

    Matched on the error message: newer Claude models report ``temperature`` as deprecated in
    the 400 body (``` `temperature` is deprecated for this model ```).
    """
    message = str(exc).lower()
    if _TEMPERATURE in message and _TEMPERATURE in kwargs:
        adjusted = dict(kwargs)
        adjusted.pop(_TEMPERATURE)
        return adjusted
    return None


def _to_response(message: Any, *, structured: bool) -> LLMResponse:
    """Translate an SDK message into our flat ``LLMResponse``."""
    text: str | None = None
    payload: dict[str, Any] | None = None
    for block in message.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            text = (text or "") + block.text
        elif btype == "tool_use":
            raw = block.input
            payload = raw if isinstance(raw, dict) else json.loads(raw)

    raw_usage = message.usage
    usage = Usage(
        input_tokens=getattr(raw_usage, "input_tokens", 0) or 0,
        output_tokens=getattr(raw_usage, "output_tokens", 0) or 0,
        cache_creation_input_tokens=getattr(raw_usage, "cache_creation_input_tokens", 0) or 0,
        cache_read_input_tokens=getattr(raw_usage, "cache_read_input_tokens", 0) or 0,
    )
    return LLMResponse(
        model=message.model,
        text=text,
        structured=payload if structured else None,
        stop_reason=getattr(message, "stop_reason", None),
        usage=usage,
    )
