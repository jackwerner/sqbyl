"""The release artifact (spec §11) — the single, portable JSON you ship.

This is the "brain": semantics + instructions + examples + trusted assets + judge
prompts + selection config, stamped with the held-out scorecard and the models it
was blessed on. The model, API key, and database are the "body" — injected at load
time, never baked in.

The whole ``schema_version``'d shape is the **documented, versioned public
interface**: its JSON Schema is generated from these models (see
``sqbyl_runtime.schema``), so third parties can read/generate/serve releases
without sqbyl itself.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from sqbyl_runtime.models.assets import Example, TrustedAsset
from sqbyl_runtime.models.base import SqbylModel
from sqbyl_runtime.models.judges import JudgePrompt
from sqbyl_runtime.models.selection import SelectionConfig
from sqbyl_runtime.models.semantics import TableSemantics

# Current release schema version. Bump on any breaking change to the shape below;
# the generated schema is the public contract third parties read against.
SCHEMA_VERSION = 1


class Dialect(StrEnum):
    """SQL dialects sqbyl can target. DuckDB + Postgres are first-class (M0);
    the rest land in Phase 9 behind the same dialect seam."""

    postgresql = "postgresql"
    duckdb = "duckdb"
    snowflake = "snowflake"
    bigquery = "bigquery"
    mysql = "mysql"
    sqlite = "sqlite"


class Scorecard(SqbylModel):
    """The eval result that justified promoting a version (spec §11).

    The headline ``accuracy`` is always the **held-out test** number; ``dev_*`` is
    shown beside it so a reviewer can see the overfitting gap.
    """

    benchmark: str = Field(description="Which set produced the headline number, e.g. 'test'.")
    accuracy: float = Field(ge=0.0, le=1.0)
    n: int = Field(ge=0, description="Number of questions in the headline set.")
    dev_accuracy: float | None = Field(default=None, ge=0.0, le=1.0)
    dev_n: int | None = Field(default=None, ge=0)
    human_reviewed: bool = False
    judge_human_agreement: float | None = Field(default=None, ge=0.0, le=1.0)
    cost_usd: float | None = Field(default=None, ge=0.0)
    latency_p50_ms: float | None = Field(default=None, ge=0.0)
    # role -> model id, e.g. {"agent": "claude-opus-4-8", "judge": "claude-opus-4-8"}.
    # An accuracy number is only meaningful for the model that produced it.
    blessed_with_models: dict[str, str] = Field(default_factory=dict)


class ReleaseArtifact(SqbylModel):
    """The portable, self-contained agent definition (``<name>.<tag>.json``)."""

    schema_version: int = SCHEMA_VERSION
    name: str
    tag: str
    created_at: datetime
    scorecard: Scorecard
    dialect: Dialect
    # sha256 of the schema the brain was built against; load() warns on mismatch.
    schema_fingerprint: str | None = None
    semantics: list[TableSemantics] = Field(default_factory=list)
    instructions: str = ""
    examples: list[Example] = Field(default_factory=list)
    trusted_assets: list[TrustedAsset] = Field(default_factory=list)
    # Judge prompts keyed by judge name.
    judges: dict[str, JudgePrompt] = Field(default_factory=dict)
    selection: SelectionConfig = Field(default_factory=SelectionConfig)
