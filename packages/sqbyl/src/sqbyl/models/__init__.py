"""Dev-only pydantic models (project files that never ship in a release).

Release-embedded models live in ``sqbyl_runtime.models``; these depend on them.
"""

from __future__ import annotations

from sqbyl.models.attention import (
    AttentionQueue,
    Decision,
    DecisionKind,
    ReadinessSignal,
)
from sqbyl.models.benchmarks import BenchmarkQuestion, MatchMode
from sqbyl.models.candidates import (
    EVIDENCE_ROW_CAP,
    Candidate,
    CandidateStatus,
    DroppedCandidate,
    DropReason,
    ExecutionEvidence,
    SynthResult,
)
from sqbyl.models.coach import (
    LAYER_PREFERENCE,
    PROSE_FILE,
    CoachEdit,
    CoachLayer,
    CoachProposal,
    CoachReport,
)
from sqbyl.models.judges import (
    ALL_JUDGES,
    GOLD_MISMATCH_JUDGES,
    JUDGE_ANSWER_QUALITY,
    JUDGE_COMPLETENESS,
    JUDGE_LOGICAL_ACCURACY,
    JUDGE_SEMANTIC_EQUIVALENCE,
    NO_GOLD_JUDGES,
    JudgeVerdict,
)
from sqbyl.models.kpis import (
    KpiReport,
    PerformanceKpis,
    QualityKpis,
    UnitEconomics,
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
    CalibrationRecord,
    JudgeAgreement,
    OverfittingSignal,
    QuestionResult,
    RunDiff,
    ScoredRun,
    ScorerResult,
    Verdict,
)

__all__ = [
    "ALL_JUDGES",
    "EVIDENCE_ROW_CAP",
    "GOLD_MISMATCH_JUDGES",
    "LAYER_PREFERENCE",
    "PROSE_FILE",
    "JUDGE_ANSWER_QUALITY",
    "JUDGE_COMPLETENESS",
    "JUDGE_LOGICAL_ACCURACY",
    "JUDGE_SEMANTIC_EQUIVALENCE",
    "MODEL_ROLES",
    "NO_GOLD_JUDGES",
    "SCORER_ASSET_ROUTING",
    "SCORER_RESULT_CORRECTNESS",
    "SCORER_SCHEMA_ACCURACY",
    "SCORER_SYNTAX_VALIDITY",
    "AttentionQueue",
    "AutomationConfig",
    "BenchmarkQuestion",
    "MatchMode",
    "Decision",
    "DecisionKind",
    "CalibrationRecord",
    "Candidate",
    "CandidateStatus",
    "CoachEdit",
    "CoachLayer",
    "CoachProposal",
    "CoachReport",
    "DatabaseConfig",
    "DefaultsConfig",
    "DropReason",
    "DroppedCandidate",
    "ExecutionEvidence",
    "JudgeAgreement",
    "JudgeVerdict",
    "KpiReport",
    "ModelConfig",
    "OverfittingSignal",
    "PerformanceKpis",
    "QualityKpis",
    "QuestionResult",
    "ReadinessSignal",
    "RunDiff",
    "ScoredRun",
    "ScorerResult",
    "SqbylManifest",
    "SynthResult",
    "UnitEconomics",
    "Verdict",
]
