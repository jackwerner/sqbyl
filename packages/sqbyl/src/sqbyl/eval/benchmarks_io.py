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
from sqbyl.yamlio import load_yaml


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
        raise FileNotFoundError(
            f"no benchmarks/{split.value}.yaml in {project.root} "
            f"(run `sqbyl synth` to build the dev set)"
        )
    raw = load_yaml(path.read_text()) or []
    return [BenchmarkQuestion.model_validate(item) for item in raw]


def load_dev_set(project: Project) -> list[BenchmarkQuestion]:
    """The iteration (dev) set — the **only** benchmark set synth/coach/optimize may read.

    Hard-wired to :attr:`Split.dev`: there is deliberately no way to ask this function for
    the held-out test set (invariant 3).
    """
    return _read_set(project, Split.dev)
