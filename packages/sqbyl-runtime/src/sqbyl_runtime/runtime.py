"""Loading a shipped release — ``load()`` + ``ask()`` (spec §11, plan 8.2).

The production surface. A release JSON is the **brain**; the model, API key, and
database are the **body**, injected here and never baked in. ``load()`` gives back an
:class:`Agent` that feels like any other model object — ``agent.ask(q)`` runs the same
stateless pipeline (:mod:`sqbyl_runtime.pipeline`) dev used, so behavior is identical in
production and in the dev repo.

On load the runtime does two cheap, **non-fatal** checks (spec §11): warn on **schema
mismatch** (a renamed/dropped table is the one thing that silently breaks a shipped
agent) and warn on **model mismatch** against the scorecard's ``blessed_with_models``
(an accuracy number is only meaningful for the model that earned it). Both respect
"I might point this at a different DB / model" while still flagging the footgun.

This is the whole embed: ``from sqbyl_runtime import load`` → three lines to add a
sqbyl-backed endpoint to an existing service. None of the dev toolkit (eval, synth,
Coach, judges, console) ships here or is importable from here — the one-way dependency
arrow enforced by import-linter.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

from sqbyl_runtime.context import ProjectKnowledge
from sqbyl_runtime.db import Database
from sqbyl_runtime.fingerprint import drifted_tables, live_schema_fingerprint
from sqbyl_runtime.llm.anthropic_client import AnthropicLLMClient
from sqbyl_runtime.llm.base import LLMClient
from sqbyl_runtime.models import ReleaseArtifact
from sqbyl_runtime.pipeline import AgentResult, ask
from sqbyl_runtime.state.traces import TraceWriter


class SchemaMismatchWarning(UserWarning):
    """The injected database's schema no longer matches the one the release was built
    against — a renamed or altered table may silently break the shipped agent (spec §11)."""


class ModelMismatchWarning(UserWarning):
    """The injected model differs from the one the release's scorecard was earned on; the
    blessed accuracy is only meaningful for the model that produced it (spec §11)."""


class Agent:
    """A loaded release, ready to answer. The runtime's "model with logs": ``ask()`` and
    structured traces, nothing that improves the agent (that all lives in dev ``sqbyl``)."""

    def __init__(
        self,
        *,
        knowledge: ProjectKnowledge,
        db: Database,
        llm: LLMClient,
        model: str,
        release: ReleaseArtifact,
        self_repair_attempts: int = 2,
        trace_writer: TraceWriter | None = None,
    ) -> None:
        self.knowledge = knowledge
        self.db = db
        self.llm = llm
        self.model = model
        self.release = release
        self._self_repair_attempts = self_repair_attempts
        self._trace_writer = trace_writer

    def ask(self, question: str) -> AgentResult:
        """Answer one question → ``AgentResult`` (plan, sql, rows, used_assets, usage,
        latency, …). A fresh, stateless pipeline run, identical to dev's."""
        return ask(
            question,
            knowledge=self.knowledge,
            db=self.db,
            llm=self.llm,
            model=self.model,
            self_repair_attempts=self._self_repair_attempts,
            trace_writer=self._trace_writer,
        )

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Agent:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def load(
    release: str | Path | ReleaseArtifact,
    *,
    db: str | Database,
    model: str,
    api_key: str | None = None,
    llm: LLMClient | None = None,
    read_only: bool = True,
    self_repair_attempts: int = 2,
    trace_writer: TraceWriter | None = None,
    warn: bool = True,
) -> Agent:
    """Load a release and inject the body (spec §11).

    ``release`` is a path to a release JSON or an already-parsed :class:`ReleaseArtifact`.
    ``db`` is a connection URL (``env:`` indirection and bare DuckDB paths both work) or an
    already-open :class:`Database`. ``model`` is the agent model to run under — swap Claude
    for anything the :class:`LLMClient` seam supports by passing your own ``llm``; otherwise
    the real Anthropic client is built from ``api_key`` (or ``$ANTHROPIC_API_KEY``).

    Emits non-fatal :class:`SchemaMismatchWarning` / :class:`ModelMismatchWarning` when the
    injected DB or model has drifted from what the release was built and blessed on. Pass
    ``warn=False`` to silence *those advisory drift warnings* (e.g. an intentional model swap
    you've already vetted). It does **not** silence the writable-credential safety warning —
    that's a data-safety control of a different class (a prod credential that can drop tables),
    always emitted on connect regardless of ``warn``. Silence that one deliberately by opening
    the :class:`Database` yourself (with its own ``warn=``) and passing it in.
    """
    artifact = _load_release(release)
    knowledge = ProjectKnowledge.from_release(artifact)
    # The privilege check always warns: a write-capable prod credential is a safety issue, not
    # a drift nicety, so it must not be silenceable via the advisory-drift flag.
    database = (
        db
        if isinstance(db, Database)
        else Database.connect(db, dialect=artifact.dialect, read_only=read_only, warn=True)
    )

    if warn:
        _warn_model_mismatch(artifact, model)
        _warn_schema_mismatch(artifact, database)

    client = llm if llm is not None else AnthropicLLMClient(api_key=api_key)
    return Agent(
        knowledge=knowledge,
        db=database,
        llm=client,
        model=model,
        release=artifact,
        self_repair_attempts=self_repair_attempts,
        trace_writer=trace_writer,
    )


def _load_release(release: str | Path | ReleaseArtifact) -> ReleaseArtifact:
    if isinstance(release, ReleaseArtifact):
        return release
    text = Path(release).read_text()
    return ReleaseArtifact.model_validate(json.loads(text))


def _warn_model_mismatch(release: ReleaseArtifact, model: str) -> None:
    blessed = release.scorecard.blessed_with_models.get("agent")
    if blessed is None:
        # The scorecard was never tied to an agent model — the least-certain case, not a
        # match. Say so distinctly rather than staying silent (which reads as "fine").
        warnings.warn(
            f"this release's scorecard isn't tied to any blessed agent model, so its "
            f"{release.scorecard.accuracy:.0%} held-out accuracy can't be attributed to the "
            f"model you're running ({model!r}) — re-eval to bless a model.",
            ModelMismatchWarning,
            stacklevel=3,
        )
    elif blessed != model:
        warnings.warn(
            f"loading under {model!r}, but the release's {release.scorecard.accuracy:.0%} "
            f"held-out score was earned on {blessed!r} — the scorecard is only meaningful for "
            "the model that produced it; re-eval on this model to re-bless it.",
            ModelMismatchWarning,
            stacklevel=3,
        )


def _warn_schema_mismatch(release: ReleaseArtifact, db: Database) -> None:
    """Recompute the live DB's fingerprint over the brain's tables and warn if it drifted
    from the release's stamped ``schema_fingerprint``, naming the specific tables that moved so
    the warning is actionable. A ``None`` fingerprint (legacy release) can't be compared, so we
    skip silently."""
    if release.schema_fingerprint is None:
        return
    live = live_schema_fingerprint(db, release.semantics)
    if live != release.schema_fingerprint:
        drifted = drifted_tables(db, release.semantics)
        which = f" ({', '.join(drifted)})" if drifted else ""
        warnings.warn(
            f"the database schema has changed since this release was built{which} — a renamed, "
            "dropped, or altered table the agent references means it may generate SQL against "
            "columns that no longer exist. Re-introspect and re-release against this DB.",
            SchemaMismatchWarning,
            stacklevel=3,
        )
