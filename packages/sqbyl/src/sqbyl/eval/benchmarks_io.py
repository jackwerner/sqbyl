"""Loading benchmark sets — and the **dev side** of the dev/test boundary (invariant 3).

The held-out ``test.yaml`` must be structurally unreachable by anything but ``eval`` and
humans: synth writes only ``dev``; coach/optimize read only ``dev`` (spec §3 #3, plan
3.4). This module owns the *safe* half — :func:`load_dev_set`, which is **hard-wired to
the dev split**. There is no ``split`` parameter on it, so a coach/synth/optimize code
path that calls it *cannot* receive ``test.yaml`` even by mistake.

The held-out set lives behind a separate door — :mod:`sqbyl.eval.heldout` — so that when
the Coach/synth/optimizer modules land (Phases 4–8) an import-linter contract can forbid
them from importing it at all. Keeping the two loaders in two modules makes "they don't
even receive test.yaml" a property CI can enforce, not a convention.

The generic split reader is intentionally **private** (:func:`_read_set`): the only
*public* reader this dev-safe module exposes is :func:`load_dev_set`, so a coach/synth/
optimize import of this module gets no split-taking function to reach ``test.yaml`` with.
An import-linter contract also forbids this module from importing :mod:`sqbyl.eval.heldout`
(so the dev-safe surface can never reach the held-out door).
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from sqbyl.models import BenchmarkQuestion
from sqbyl.project import Project
from sqbyl.yamlio import dump_yaml, load_yaml


class Split(StrEnum):
    """The two benchmark splits. ``dev`` is the iteration set; ``test`` is held out."""

    dev = "dev"
    test = "test"


def benchmark_path(project: Project, split: Split) -> Path:
    return project.root / "benchmarks" / f"{split.value}.yaml"


def _read_set(project: Project, split: Split) -> list[BenchmarkQuestion]:
    """Parse one benchmark split into validated questions.

    **Private on purpose.** The dev-safe public surface of this module is only
    :func:`load_dev_set`; the held-out door (:mod:`sqbyl.eval.heldout`) reaches this via
    the private name, so coach/synth/optimize importing this module get no split-taking
    reader to smuggle ``test.yaml`` through (invariant 3).
    """
    path = benchmark_path(project, split)
    if not path.exists():
        # Split-aware guidance: `synth` only ever writes dev; the held-out test set is
        # hand-authored (never synthesized, invariant 3), so pointing a test caller at `synth`
        # is categorically wrong (finding #8).
        if split is Split.test:
            raise FileNotFoundError(
                f"no benchmarks/test.yaml in {project.root} — the held-out set is hand-authored "
                "(never synthesized, invariant 3); add questions to benchmarks/test.yaml"
            )
        raise FileNotFoundError(
            f"no benchmarks/dev.yaml in {project.root} (run `sqbyl synth` to build the dev set)"
        )
    raw = load_yaml(path.read_text()) or []
    return [BenchmarkQuestion.model_validate(item) for item in raw]


def load_dev_set(project: Project) -> list[BenchmarkQuestion]:
    """The iteration (dev) set — the **only** benchmark set synth/coach/optimize may read.

    Hard-wired to :attr:`Split.dev`: there is deliberately no way to ask this function for
    the held-out test set (invariant 3).
    """
    return _read_set(project, Split.dev)


def _read_dev_set_lenient(project: Project) -> list[BenchmarkQuestion]:
    """Dev questions, or ``[]`` when ``dev.yaml`` doesn't exist yet (first synth run)."""
    if not benchmark_path(project, Split.dev).exists():
        return []
    return _read_set(project, Split.dev)


def dev_set_size(project: Project) -> int:
    """How many dev questions exist (0 before the first synth) — the **public**, dev-only
    surface for callers that just need the count (e.g. `sqbyl init` deciding whether to
    synthesize). Like the rest of this module's public API it cannot reach ``test.yaml``."""
    return len(_read_dev_set_lenient(project))


def _dump_questions(questions: list[BenchmarkQuestion]) -> str:
    """Serialize benchmark questions to a YAML sequence, dropping empty/default fields."""
    data = [q.model_dump(exclude_none=True, exclude_defaults=True) for q in questions]
    return dump_yaml(data)


def append_to_dev_set(
    project: Project, questions: list[BenchmarkQuestion]
) -> list[BenchmarkQuestion]:
    """Append accepted questions to ``benchmarks/dev.yaml`` — the **only** writer.

    Hard-wired to :attr:`Split.dev` (there is no ``split`` argument), so synth and the
    review console can only ever grow the dev set — never the held-out ``test.yaml``
    (invariant 3). Questions whose ``id`` already exists are skipped so re-accepting is
    idempotent and hand edits are never clobbered; the accepted (newly added) questions
    are returned. Appends as text so any hand-authored comments/order above survive.
    """
    existing_ids = {q.id for q in _read_dev_set_lenient(project)}
    added = [q for q in questions if q.id not in existing_ids]
    if not added:
        return []
    path = benchmark_path(project, Split.dev)
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = ""
    if path.exists() and path.stat().st_size > 0:
        current = path.read_text()
        # A blank line separates the appended block from prior content for readability.
        prefix = (current if current.endswith("\n") else current + "\n") + "\n"
    path.write_text(prefix + _dump_questions(added))
    return added
