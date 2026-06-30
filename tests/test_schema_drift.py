"""Phase 0.2 — the checked-in release schema must not drift from the models.

The generated JSON Schema is the published public interface (spec §11). If a model
changes, ``sqbyl schema export`` must be re-run and the result committed. This test
is the guard that makes that non-optional.
"""

from __future__ import annotations

from sqbyl_runtime.schema import RELEASE_SCHEMA_PATH, schema_text


def test_checked_in_schema_matches_models() -> None:
    assert RELEASE_SCHEMA_PATH.exists(), (
        "schemas/release.schema.json is missing — run `uv run sqbyl schema export`"
    )
    on_disk = RELEASE_SCHEMA_PATH.read_text()
    generated = schema_text()
    assert on_disk == generated, (
        "release schema is stale — run `uv run sqbyl schema export` and commit the result"
    )


def test_schema_is_versioned() -> None:
    text = schema_text()
    assert '"schema_version"' in text
