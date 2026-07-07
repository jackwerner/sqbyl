"""The project manifest — ``sqbyl.yaml`` (spec §4).

Dev-side configuration: the db connection, the per-role model pinning, automation
toggles, and defaults. This is *not* part of a release (the model + DB are injected
at load time), so it lives in the dev ``sqbyl`` package.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from sqbyl_runtime.llm.factory import SUPPORTED_PROVIDERS
from sqbyl_runtime.models import Dialect, SelectionConfig, SqbylModel

# Roles that can pin their own model; each falls back to ``ModelConfig.default``.
MODEL_ROLES = (
    "agent",
    "selection",
    "orchestrator",
    "synth",
    "coach",
    "judge",
    "narrate",
)


class DatabaseConfig(SqbylModel):
    """How to reach the database. Credentials use ``env:`` indirection, never literals."""

    dialect: Dialect
    url: str = Field(description="Connection URL; prefer 'env:DATABASE_URL' indirection.")
    read_only: bool = Field(
        default=True,
        description="Refuse non-SELECT; warn if the credential can write (spec §13).",
    )


class ModelConfig(SqbylModel):
    """One provider + key, many roles. A project picks a single ``provider`` and uses it for
    everything (no mixing); each role's model is independently pinnable and unset roles fall
    back to ``default`` (spec §4)."""

    provider: str = Field(
        default="anthropic",
        description=f"LLM backend; one of {', '.join(SUPPORTED_PROVIDERS)}. Used for every role.",
    )
    api_key: str = Field(
        description="Prefer 'env:' indirection, e.g. env:ANTHROPIC_API_KEY or env:OPENAI_API_KEY.",
    )
    base_url: str | None = Field(
        default=None,
        description=(
            "Optional alternate provider endpoint (corporate proxy / AI gateway). "
            "Plain URL or 'env:VAR'. Unset uses the provider's default."
        ),
    )
    default: str = "claude-opus-4-8"
    agent_model: str | None = None
    selection_model: str | None = None
    orchestrator_model: str | None = None
    synth_model: str | None = None
    coach_model: str | None = None
    judge_model: str | None = None
    narrate_model: str | None = None

    @model_validator(mode="after")
    def _check_provider(self) -> ModelConfig:
        if self.provider not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"unknown provider {self.provider!r}; supported: {', '.join(SUPPORTED_PROVIDERS)}"
            )
        return self

    def for_role(self, role: str, override: str | None = None) -> str:
        """Resolve the model id for a role: an explicit per-role pin wins, else ``override``
        (e.g. ``sqbyl init --model``), else ``default``.

        ``override`` lets a global ``--model`` reprice/redirect *every* role at once — synth
        and judge included — while still honoring a role a user deliberately pinned in
        ``sqbyl.yaml``. With ``override=None`` this is the original default-fallback behavior."""
        if role not in MODEL_ROLES:
            raise ValueError(f"unknown model role {role!r}; expected one of {MODEL_ROLES}")
        pinned: str | None = getattr(self, f"{role}_model")
        return pinned or override or self.default


class AutomationConfig(SqbylModel):
    """Whether the loop runs itself after an eval, or waits to be asked (spec §4).

    Defaults match the spec: on. When off, sqbyl still surfaces a one-line nudge
    after each run so the capability stays discoverable.
    """

    auto_judge: bool = True
    auto_coach: bool = True


class DefaultsConfig(SqbylModel):
    """Project-wide knobs (spec §4)."""

    max_tables_warn: int = Field(default=7, ge=1, description="'Small space' nudge threshold.")
    self_repair_attempts: int = Field(default=2, ge=0)
    prompt_caching: bool = True
    readiness_target: float = Field(
        default=0.95,
        ge=0.0,
        le=1.0,
        description="Accuracy the readiness meter counts down to — 'shippable' (spec §5.5).",
    )
    auto_apply_threshold: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description=(
            "Machine decisions at/above this confidence are applied without asking, with "
            "one-click undo (spec §5.5). An unvalidated heuristic — set to 1.0 to require a "
            "human on everything until it's calibrated against accept/reject rates."
        ),
    )


class SqbylManifest(SqbylModel):
    """The whole ``sqbyl.yaml``."""

    name: str
    description: str | None = None
    database: DatabaseConfig
    model: ModelConfig
    automation: AutomationConfig = Field(default_factory=AutomationConfig)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    # Context selection (spec §5.1). Defaults to include-all (the small-project posture);
    # set ``strategy: lexical|llm|llm_lexical`` + ``max_tables`` for large schemas (Phase 9).
    selection: SelectionConfig = Field(default_factory=SelectionConfig)

    @model_validator(mode="after")
    def _warn_keys(self) -> SqbylManifest:
        # Names are referenced in run reports and the release; keep them non-empty.
        if not self.name.strip():
            raise ValueError("manifest 'name' must be non-empty")
        return self
