"""Phase 0.2 — validation rules the models enforce (invariant 2: no hand-validation)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sqbyl.models import BenchmarkQuestion, ModelConfig, SqbylManifest
from sqbyl.models.manifest import DatabaseConfig
from sqbyl_runtime.models import Dialect, Profile


def test_unknown_field_is_rejected() -> None:
    # extra="forbid" turns a typo'd YAML key into a loud error, not a dropped value.
    with pytest.raises(ValidationError):
        Profile(nulls=0.0, distnct=3)  # type: ignore[call-arg]


def test_null_fraction_bounds() -> None:
    with pytest.raises(ValidationError):
        Profile(nulls=1.5)


def test_benchmark_requires_exactly_one_gold() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        BenchmarkQuestion(id="q", question="?")  # neither
    with pytest.raises(ValidationError, match="exactly one"):
        BenchmarkQuestion(id="q", question="?", gold_sql="SELECT 1", gold_asset="mrr")  # both
    # Each alone is fine.
    assert BenchmarkQuestion(id="q", question="?", gold_sql="SELECT 1").gold_asset is None
    assert BenchmarkQuestion(id="q", question="?", gold_asset="mrr").gold_sql is None


def test_model_role_resolution_falls_back_to_default() -> None:
    cfg = ModelConfig(api_key="env:KEY", default="claude-opus-4-8", judge_model="claude-haiku-4-5")
    assert cfg.for_role("judge") == "claude-haiku-4-5"  # pinned
    assert cfg.for_role("agent") == "claude-opus-4-8"  # falls back to default
    with pytest.raises(ValueError, match="unknown model role"):
        cfg.for_role("nonsense")


def test_manifest_defaults_match_spec() -> None:
    m = SqbylManifest(
        name="x",
        database=DatabaseConfig(dialect=Dialect.duckdb, url="env:DATABASE_URL"),
        model=ModelConfig(api_key="env:KEY"),
    )
    # Spec §4 defaults — automation on, small-space nudge at 7, read-only on.
    assert m.automation.auto_judge is True
    assert m.automation.auto_coach is True
    assert m.defaults.max_tables_warn == 7
    assert m.defaults.self_repair_attempts == 2
    assert m.database.read_only is True
