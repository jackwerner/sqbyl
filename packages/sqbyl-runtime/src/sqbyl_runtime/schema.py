"""Generate the release JSON Schema from the pydantic models (invariant 2).

The ``ReleaseArtifact`` schema *is* the documented, versioned public interface
promised in spec §11. It is **generated**, never hand-maintained: the checked-in
``schemas/release.schema.json`` is produced from this and a CI test fails if it
drifts from the models.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqbyl_runtime.models import ReleaseArtifact

# Path to the checked-in schema, relative to the repo root. Resolved from this
# file so it works regardless of cwd: packages/sqbyl-runtime/src/sqbyl_runtime/ -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[4]
RELEASE_SCHEMA_PATH = _REPO_ROOT / "schemas" / "release.schema.json"


def release_json_schema() -> dict[str, Any]:
    """The JSON Schema for a release artifact, generated from the model."""
    schema = ReleaseArtifact.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "sqbyl release artifact"
    return schema


def schema_text() -> str:
    """Deterministic, pretty-printed schema text (stable key order, trailing newline)."""
    return json.dumps(release_json_schema(), indent=2, sort_keys=True) + "\n"


def write_release_schema(path: Path | None = None) -> Path:
    """Write the generated schema to disk. Returns the path written."""
    target = path or RELEASE_SCHEMA_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(schema_text())
    return target
