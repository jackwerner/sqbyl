"""The release compiler — ``sqbyl release create`` (spec §11, plan 8.1).

Compile the working project into the single, portable :class:`ReleaseArtifact` JSON
you ship: the **brain** (semantics + instructions + examples + trusted assets + judge
prompts + selection config), stamped with the **held-out scorecard**, the models it
was blessed on, and the schema fingerprint it was built against. The model, API key,
and database are the **body** — injected at :func:`sqbyl_runtime.load` time, never
baked in.

This is a **$0 file operation**: it never connects to the database or spends a token.
The headline accuracy is read from the most recent persisted **held-out test** run —
``eval`` is the one command allowed to touch ``test.yaml`` (invariant 3); ``release``
only stamps the number that run already produced. So the flow is ``sqbyl eval test``
→ ``sqbyl release create``. (This module therefore never imports
:mod:`sqbyl.eval.heldout`; it reads a :class:`~sqbyl.models.ScoredRun` off disk.)
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqbyl.calibration_io import judge_agreement
from sqbyl.eval.report import load_runs
from sqbyl.models.judges import ALL_JUDGES
from sqbyl.models.runs import ScoredRun
from sqbyl.project import Project
from sqbyl.projectfiles import (
    load_examples,
    load_instructions,
    load_knowledge,
    load_semantics,
    load_trusted_assets,
)
from sqbyl.stats import percentile
from sqbyl_runtime.fingerprint import fingerprint_knowledge
from sqbyl_runtime.models import (
    JudgePrompt,
    ReleaseArtifact,
    Scorecard,
)
from sqbyl_runtime.state.layout import SqbylPaths

# A dev↔test gap wider than this on the shipped scorecard is surfaced as an overfitting
# warning (spec §11): the loop may have tuned the dev set rather than generalizing.
OVERFITTING_GAP = 0.1


class ReleaseError(Exception):
    """A release could not be built — most often, no held-out eval to headline, or a
    held-out eval that scored a *different* version of the project than the one shipping."""


def build_release(
    project: Project,
    tag: str,
    *,
    created_at: datetime | None = None,
    judge_prompts: dict[str, str] | None = None,
) -> ReleaseArtifact:
    """Assemble the project's current files + the latest held-out scorecard into a
    :class:`ReleaseArtifact`. Pure: reads files and persisted runs, nothing else.

    Requires a persisted **test** run (run ``sqbyl eval test`` first) — the headline
    number is always the held-out one, never dev (spec §11). ``judge_prompts`` lets a
    caller inject the resolved prompts (the CLI passes ``load_judge_prompts``); left
    ``None``, the bundled defaults are embedded so a release is always self-describing.
    """
    tag = tag.strip()
    if not tag:
        raise ReleaseError("a release needs a non-empty --tag (e.g. v1)")

    semantics = load_semantics(project)
    # The fingerprint of the brain we're about to ship — the scorecard's held-out run must
    # have scored *this* brain, or its accuracy is a number some other version earned.
    shipped_fingerprint = fingerprint_knowledge(load_knowledge(project))
    test = _verified_test_run(project, shipped_fingerprint=shipped_fingerprint)
    prompts = judge_prompts if judge_prompts is not None else _default_judge_prompts()
    return ReleaseArtifact(
        name=project.manifest.name,
        tag=tag,
        created_at=created_at or datetime.now(UTC),
        scorecard=_scorecard(project, test),
        dialect=project.manifest.database.dialect,
        # The **live** schema fingerprint the held-out run recorded — computed from the DB's
        # own inspector, so load() can recompute it identically and warn only on real drift.
        # (``None`` on a legacy run; the YAML-derived hash would false-positive on healthy DBs.)
        schema_fingerprint=test.schema_fingerprint,
        semantics=semantics,
        instructions=load_instructions(project),
        examples=load_examples(project),
        trusted_assets=load_trusted_assets(project),
        judges={name: JudgePrompt(name=name, prompt=prompts[name]) for name in ALL_JUDGES},
        # Ship the project's own selection config so the runtime compiles context exactly
        # as dev did — include-all for small projects, lexical/LLM shortlisting past
        # ``max_tables`` for large schemas (spec §5.1).
        selection=project.manifest.selection,
    )


def _verified_test_run(project: Project, *, shipped_fingerprint: str) -> ScoredRun:
    """The latest held-out **test** run, after proving it scored the brain being shipped.

    Refuses a **stale** scorecard — a held-out run that scored a different brain than the one
    shipping (``shipped_fingerprint``) — so the headline number can always be tied to the
    files in the release. A run predating fingerprinting (``knowledge_fingerprint`` is
    ``None``) can't be verified either way; it's allowed through, and the resulting scorecard
    carries a ``None`` fingerprint so the CLI can flag the number as unverified.
    """
    test = _latest(load_runs(SqbylPaths(project.root), split="test"))
    if test is None:
        raise ReleaseError(
            "no held-out eval to headline — run `sqbyl eval test` before releasing "
            "(the release's accuracy is always the held-out number, never dev; spec §11)"
        )
    if test.knowledge_fingerprint is not None and test.knowledge_fingerprint != shipped_fingerprint:
        raise ReleaseError(
            "the latest held-out eval scored a different version of the project than the one "
            "you're releasing (its context files have changed since) — re-run `sqbyl eval test` "
            "so the scorecard's accuracy is the one these files actually earned (spec §11)"
        )
    return test


def _scorecard(project: Project, test: ScoredRun) -> Scorecard:
    """The scorecard that justifies promotion (spec §11): the held-out **test** accuracy as
    the headline, with the latest **dev** number beside it so a reviewer sees the gap."""
    dev = _latest(load_runs(SqbylPaths(project.root), split="dev"))
    latencies = [r.latency_ms for r in test.results]
    low, high = test.accuracy_ci()
    # Judge agreement is scoped to the **headline split** — a test-set release must not
    # advertise reliability derived from dev reviews — and carried with its denominator.
    agreement = judge_agreement(project, split="test")
    return Scorecard(
        benchmark="test",
        accuracy=test.accuracy,
        n=test.total,
        accuracy_low=low,
        accuracy_high=high,
        n_manual_review=test.n_manual_review,
        dev_accuracy=dev.accuracy if dev is not None else None,
        dev_n=dev.total if dev is not None else None,
        # Whether a human worked the held-out review pile at all (honest False when nobody
        # has). The headline accuracy itself is deterministic and needs no review to trust.
        human_reviewed=test.n_reviewed > 0,
        judge_human_agreement=agreement.rate,
        judge_human_agreement_n=agreement.n,
        cost_usd=test.total_cost_usd,
        latency_p50_ms=percentile(latencies, 50) if latencies else None,
        # Provenance: reproduce the number from the frozen clock, the judge few-shot in
        # force, and the brain that scored it (spec §7/§11).
        as_of=test.as_of,
        judge_calibration=test.judge_calibration,
        knowledge_fingerprint=test.knowledge_fingerprint,
        # The number is only meaningful for the model that produced it (spec §11).
        blessed_with_models=dict(test.models),
    )


def _latest(runs: list[ScoredRun]) -> ScoredRun | None:
    # load_runs returns oldest-first; the newest run is the current version's score.
    return runs[-1] if runs else None


def _default_judge_prompts() -> dict[str, str]:
    """The bundled default judge prompts, without needing a project on disk — so a release
    always embeds a prompt for every judge even when the project overrode none."""
    from sqbyl.eval.judges import _DEFAULT_PROMPTS

    return {name: _DEFAULT_PROMPTS[name] for name in ALL_JUDGES}


def overfitting_gap(release: ReleaseArtifact) -> float | None:
    """The dev↔test accuracy gap on the shipped scorecard, or ``None`` if dev is unknown.

    A large positive gap means the loop may have overfit the dev set rather than
    generalizing (spec §11) — the CLI surfaces it as a non-fatal warning."""
    dev = release.scorecard.dev_accuracy
    if dev is None:
        return None
    return dev - release.scorecard.accuracy


def release_filename(release: ReleaseArtifact) -> str:
    """The conventional ``<name>.<tag>.json`` (spec §11: ``revenue-analytics.v3.json``)."""
    return f"{release.name}.{release.tag}.json"


def write_release(release: ReleaseArtifact, out: str | Path) -> Path:
    """Write the release JSON to ``out`` (a file, or a directory to name it conventionally).
    Returns the path written."""
    out = Path(out)
    path = out / release_filename(release) if out.is_dir() else out
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(release.model_dump_json(indent=2) + "\n")
    return path
