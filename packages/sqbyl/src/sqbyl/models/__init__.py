"""Dev-only pydantic models (project files that never ship in a release).

Release-embedded models live in ``sqbyl_runtime.models``; these depend on them.
"""

from __future__ import annotations

from sqbyl.models.benchmarks import BenchmarkQuestion
from sqbyl.models.manifest import (
    MODEL_ROLES,
    AutomationConfig,
    DatabaseConfig,
    DefaultsConfig,
    ModelConfig,
    SqbylManifest,
)
from sqbyl.models.runs import (
    SCORER_ASSET_ROUTING,
    SCORER_RESULT_CORRECTNESS,
    SCORER_SCHEMA_ACCURACY,
    SCORER_SYNTAX_VALIDITY,
    OverfittingSignal,
    QuestionResult,
    RunDiff,
    ScoredRun,
    ScorerResult,
    Verdict,
)

__all__ = [
    "MODEL_ROLES",
    "SCORER_ASSET_ROUTING",
    "SCORER_RESULT_CORRECTNESS",
    "SCORER_SCHEMA_ACCURACY",
    "SCORER_SYNTAX_VALIDITY",
    "AutomationConfig",
    "BenchmarkQuestion",
    "DatabaseConfig",
    "DefaultsConfig",
    "ModelConfig",
    "OverfittingSignal",
    "QuestionResult",
    "RunDiff",
    "ScoredRun",
    "ScorerResult",
    "SqbylManifest",
    "Verdict",
]
