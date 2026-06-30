"""Shared pydantic base for every sqbyl model.

pydantic v2 is the single schema authority (invariant 2): every project-file and
release-artifact shape is a model here, so validation, (de)serialization, and the
generated JSON Schema all flow from one place. ``extra="forbid"`` makes typos in
hand-edited YAML a loud error rather than a silently-dropped key.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SqbylModel(BaseModel):
    """Base for all sqbyl models: strict, no surprise fields."""

    model_config = ConfigDict(
        extra="forbid",
        # Trim incidental whitespace from hand-authored YAML strings.
        str_strip_whitespace=True,
        # Validate on assignment so mutating a loaded model can't bypass schema.
        validate_assignment=True,
    )
