"""Phase 0.2 — round-trip every project-file and release-artifact shape.

For each model: build an instance, dump to JSON/dict, reload, and assert equality.
This is the contract that the pydantic models own (de)serialization (invariant 2).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import yaml

from sqbyl.models import (
    AutomationConfig,
    BenchmarkQuestion,
    DatabaseConfig,
    DefaultsConfig,
    ModelConfig,
    SqbylManifest,
)
from sqbyl_runtime.models import (
    AssetParam,
    Column,
    Dialect,
    Example,
    Filter,
    Join,
    JudgePrompt,
    Measure,
    Profile,
    ReleaseArtifact,
    Scorecard,
    SelectionConfig,
    TableSemantics,
    TrustedAsset,
)


def _roundtrip_json(model):  # type: ignore[no-untyped-def]
    cls = type(model)
    return cls.model_validate_json(model.model_dump_json())


def _roundtrip_yaml(model):  # type: ignore[no-untyped-def]
    cls = type(model)
    # Through YAML the way project files travel: dump (json-mode for enums/dates) → yaml → load.
    as_yaml = yaml.safe_dump(model.model_dump(mode="json"))
    return cls.model_validate(yaml.safe_load(as_yaml))


SAMPLES = [
    Profile(nulls=0.0, distinct=3, min=0, max=4200000, p50=4999.0, p95=28900.0, sampled=True),
    Profile(nulls=0.0, min="2019-02-01", max="2026-06-29"),
    Column(
        name="amount_cents",
        type="bigint",
        description="Order total in cents.",
        sample_values=[100, 200, 300],
        profile=Profile(nulls=0.0, min=0, max=4200000),
    ),
    # The PII opt-out is a modeled shape: `profile: false` must round-trip.
    Column(name="email", type="text", profile=False),
    Join(
        to="analytics.customers",
        type="many_to_one",
        on="orders.customer_id = customers.customer_id",
        confidence=0.4,
    ),
    Measure(name="net_revenue", description="Revenue net of refunds.", sql="SUM(...)/100.0"),
    Filter(name="last_quarter", sql="created_at >= date_trunc('quarter', now())"),
    TableSemantics(
        table="analytics.orders",
        description="One row per confirmed order.",
        synonyms=["purchases", "sales"],
        columns=[Column(name="order_id", type="bigint")],
        joins=[Join(to="analytics.customers", type="many_to_one", on="a = b")],
        measures=[Measure(name="net_revenue", sql="SUM(x)")],
        filters=[Filter(name="recent", sql="created_at > now()")],
    ),
    Example(question="net revenue last month?", sql="SELECT 1", tags=["revenue"]),
    AssetParam(name="month", type="date"),
    TrustedAsset(
        name="monthly_recurring_revenue",
        description="Official MRR.",
        params=[AssetParam(name="month", type="date")],
        sql="SELECT ...",
    ),
    JudgePrompt(name="semantic_equivalence", prompt="Compare the two queries..."),
    SelectionConfig(strategy="llm_lexical", max_tables=30, value_matching=True),
    Scorecard(
        benchmark="test",
        accuracy=0.94,
        n=50,
        dev_accuracy=0.97,
        dev_n=120,
        human_reviewed=True,
        judge_human_agreement=0.97,
        blessed_with_models={"agent": "claude-opus-4-8", "judge": "claude-opus-4-8"},
    ),
    DatabaseConfig(dialect=Dialect.postgresql, url="env:DATABASE_URL"),
    ModelConfig(api_key="env:ANTHROPIC_API_KEY", agent_model="claude-opus-4-8"),
    AutomationConfig(),
    DefaultsConfig(),
    BenchmarkQuestion(id="q1", question="how much revenue?", gold_sql="SELECT 1"),
    BenchmarkQuestion(id="q2", question="MRR?", gold_asset="monthly_recurring_revenue"),
]


@pytest.mark.parametrize("model", SAMPLES, ids=lambda m: type(m).__name__)
def test_json_roundtrip(model) -> None:  # type: ignore[no-untyped-def]
    assert _roundtrip_json(model) == model


@pytest.mark.parametrize("model", SAMPLES, ids=lambda m: type(m).__name__)
def test_yaml_roundtrip(model) -> None:  # type: ignore[no-untyped-def]
    assert _roundtrip_yaml(model) == model


def test_full_manifest_roundtrip() -> None:
    manifest = SqbylManifest(
        name="revenue-analytics",
        description="Answers revenue questions.",
        database=DatabaseConfig(dialect=Dialect.postgresql, url="env:DATABASE_URL"),
        model=ModelConfig(api_key="env:ANTHROPIC_API_KEY"),
    )
    assert _roundtrip_yaml(manifest) == manifest


def test_full_release_roundtrip() -> None:
    release = ReleaseArtifact(
        name="revenue-analytics",
        tag="v3",
        created_at=datetime(2026, 6, 29, 14, 2, tzinfo=UTC),
        dialect=Dialect.postgresql,
        schema_fingerprint="sha256:abc",
        scorecard=Scorecard(benchmark="test", accuracy=0.94, n=50),
        semantics=[TableSemantics(table="orders", columns=[Column(name="id", type="bigint")])],
        instructions="Be careful with refunds.",
        examples=[Example(question="q", sql="SELECT 1")],
        trusted_assets=[TrustedAsset(name="mrr", sql="SELECT 1")],
        judges={"semantic_equivalence": JudgePrompt(name="semantic_equivalence", prompt="...")},
    )
    assert _roundtrip_json(release) == release
    assert release.schema_version == 1
