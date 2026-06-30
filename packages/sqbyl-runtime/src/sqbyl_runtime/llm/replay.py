"""``RecordReplayLLMClient`` — capture real responses, replay them in CI (spec §9.5).

A *cassette* is a JSON file mapping request fingerprints → recorded responses.

- ``record`` mode wraps an inner (real) client: it calls through, stores the
  response, and returns it. Used once, locally, with a key.
- ``replay`` mode needs no inner client and no network: it looks the request up
  by fingerprint and returns the recorded response. This is what runs in CI.

Identical requests collide on fingerprint by design, so a captured fixture is
fully deterministic on replay.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from sqbyl_runtime.llm.base import LLMClient, LLMRequest, LLMResponse

Mode = Literal["record", "replay"]


class CassetteMissError(KeyError):
    """Raised in replay mode when a request has no recorded response."""


class RecordReplayLLMClient(LLMClient):
    def __init__(
        self,
        cassette_path: str | Path,
        *,
        mode: Mode,
        inner: LLMClient | None = None,
    ) -> None:
        self.cassette_path = Path(cassette_path)
        self.mode = mode
        self._inner = inner
        self._entries: dict[str, dict[str, Any]] = {}
        if mode == "record" and inner is None:
            raise ValueError("record mode requires an inner client to call through to")
        if self.cassette_path.exists():
            self._load()

    def _load(self) -> None:
        raw = json.loads(self.cassette_path.read_text())
        self._entries = raw.get("entries", {})

    def _save(self) -> None:
        self.cassette_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "entries": self._entries}
        self.cassette_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def complete(self, request: LLMRequest) -> LLMResponse:
        key = request.fingerprint()
        if self.mode == "replay":
            if key not in self._entries:
                raise CassetteMissError(
                    f"no recorded response for request {key[:12]}… in {self.cassette_path.name}; "
                    "re-record the cassette"
                )
            return LLMResponse.model_validate(self._entries[key]["response"])

        # record mode
        assert self._inner is not None  # guarded in __init__
        response = self._inner.complete(request)
        self._entries[key] = {
            "request": request.model_dump(mode="json"),
            "response": response.model_dump(mode="json"),
        }
        self._save()
        return response
