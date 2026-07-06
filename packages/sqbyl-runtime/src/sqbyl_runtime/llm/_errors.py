"""Provider-agnostic error helpers shared by the real LLM clients.

Both the Anthropic and OpenAI SDKs surface a rate-limit as an exception carrying
``status_code == 429`` and (usually) a ``response`` with a ``Retry-After`` header. We
match that *structurally* — by shape, not by importing either SDK — so the seam can
translate it into :class:`~sqbyl_runtime.llm.base.RateLimitError` without a hard
dependency on whichever SDK raised it (invariant 4: importing a client costs nothing).
"""

from __future__ import annotations

from typing import Any


def is_rate_limit(exc: Exception) -> bool:
    """True for an SDK 429 — matched structurally so we don't hard-import an SDK."""
    return getattr(exc, "status_code", None) == 429 or type(exc).__name__ == "RateLimitError"


def retry_after(exc: Exception) -> float | None:
    """Pull the ``Retry-After`` hint (seconds) off the SDK error's response, if any."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw: Any = headers.get("retry-after")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
