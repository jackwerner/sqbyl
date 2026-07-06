"""Build an ``LLMClient`` for the dev CLI from a project manifest.

Resolves the manifest's ``api_key`` (``env:`` indirection) for the real client, and
supports ``--replay``/``--record`` cassettes so a command can run with no key (CI,
demos) or capture a fixture once. Keeping this on the dev side means the runtime
seam stays free of CLI/manifest concerns.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqbyl.models import SqbylManifest
from sqbyl_runtime.llm.base import LLMClient
from sqbyl_runtime.llm.factory import build_provider_client
from sqbyl_runtime.llm.replay import RecordReplayLLMClient


def _resolve_env_ref(raw: str | None) -> str | None:
    """Resolve an ``env:VAR`` reference to its value; pass a plain string through."""
    if raw is None:
        return None
    raw = raw.strip()
    if raw.startswith("env:"):
        return os.environ.get(raw[len("env:") :])
    return raw or None


def _resolve_api_key(manifest: SqbylManifest) -> str | None:
    return _resolve_env_ref(manifest.model.api_key)


def build_llm_client(
    manifest: SqbylManifest,
    *,
    replay: str | Path | None = None,
    record: str | Path | None = None,
) -> LLMClient:
    """Real client by default; replay/record cassette when requested.

    ``replay`` needs no key (this is the CI path); ``record`` wraps the real client.
    """
    if replay is not None:
        return RecordReplayLLMClient(replay, mode="replay")
    real = build_provider_client(
        manifest.model.provider,
        api_key=_resolve_api_key(manifest),
        base_url=_resolve_env_ref(manifest.model.base_url),
    )
    if record is not None:
        return RecordReplayLLMClient(record, mode="record", inner=real)
    return real
