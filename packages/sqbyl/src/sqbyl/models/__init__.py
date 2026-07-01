"""Dev-only pydantic models (project files that never ship in a release).

Release-embedded models live in ``sqbyl_runtime.models``; these depend on them.
"""

from __future__ import annotations

from sqbyl.models.benchmarks import BenchmarkQuestion
from sqbyl.models.candidates import (
    EVIDENCE_ROW_CAP,
    Candidate,
    CandidateStatus,
    DroppedCandidate,
    DropReason,
    ExecutionEvidence,
    SynthResult,
)
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
    "EVIDENCE_ROW_CAP",
    "MODEL_ROLES",
    "SCORER_ASSET_ROUTING",
    "SCORER_RESULT_CORRECTNESS",
    "SCORER_SCHEMA_ACCURACY",
    "SCORER_SYNTAX_VALIDITY",
    "AutomationConfig",
    "BenchmarkQuestion",
    "Candidate",
    "CandidateStatus",
    "DatabaseConfig",
    "DefaultsConfig",
    "DropReason",
    "DroppedCandidate",
    "ExecutionEvidence",
    "ModelConfig",
    "OverfittingSignal",
    "QuestionResult",
    "RunDiff",
    "ScoredRun",
    "ScorerResult",
    "SqbylManifest",
    "SynthResult",
    "Verdict",
]
