"""Few-shot examples and trusted assets (spec §4).

``Example`` is an NL→SQL pair (``examples/*.yaml``). ``TrustedAsset`` is a vetted,
parameterized "single source of truth" query (``trusted/*.sql``) the agent is told
to prefer over ad-hoc math. Both are embedded in a release.
"""

from __future__ import annotations

from pydantic import Field

from sqbyl_runtime.models.base import SqbylModel


class Example(SqbylModel):
    """An NL question paired with its gold SQL, used as a few-shot example."""

    question: str
    sql: str
    tags: list[str] = Field(default_factory=list)


class AssetParam(SqbylModel):
    """A named parameter of a trusted asset (e.g. ``month (date)``)."""

    name: str
    type: str


class TrustedAsset(SqbylModel):
    """A vetted, parameterized query the agent should prefer (spec §4 trusted/)."""

    name: str
    description: str | None = None
    params: list[AssetParam] = Field(default_factory=list)
    sql: str
