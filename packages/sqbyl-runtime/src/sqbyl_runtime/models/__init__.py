"""Release-embedded pydantic models (the shapes that ship in a release artifact).

Dev-only models (the manifest, benchmark questions) live in the ``sqbyl`` package,
which depends on these — never the reverse.
"""

from __future__ import annotations

from sqbyl_runtime.models.assets import AssetParam, Example, TrustedAsset
from sqbyl_runtime.models.base import SqbylModel
from sqbyl_runtime.models.judges import JudgePrompt
from sqbyl_runtime.models.release import (
    SCHEMA_VERSION,
    Dialect,
    ReleaseArtifact,
    Scorecard,
)
from sqbyl_runtime.models.selection import SelectionConfig, SelectionStrategy
from sqbyl_runtime.models.semantics import (
    Column,
    Filter,
    Join,
    JoinCardinality,
    Measure,
    Profile,
    ScalarBound,
    TableSemantics,
)

__all__ = [
    "SCHEMA_VERSION",
    "AssetParam",
    "Column",
    "Dialect",
    "Example",
    "Filter",
    "Join",
    "JoinCardinality",
    "JudgePrompt",
    "Measure",
    "Profile",
    "ReleaseArtifact",
    "ScalarBound",
    "Scorecard",
    "SelectionConfig",
    "SelectionStrategy",
    "SqbylModel",
    "TableSemantics",
    "TrustedAsset",
]
