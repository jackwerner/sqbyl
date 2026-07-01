"""The held-out door — the **only** way to read ``test.yaml`` (invariant 3, plan 3.4).

``test.yaml`` is the honest, headline accuracy number; optimizing or measuring against it
is training on the test set. So the held-out set is reachable *only* through this module,
which ``eval`` (and humans) import — and which the Coach / synth / optimizer must never
import. That non-import is the code-level boundary: when those modules land an
import-linter contract forbids ``sqbyl.coach``/``sqbyl.synth``/``sqbyl.optimize`` from
importing ``sqbyl.eval.heldout`` (see plan 3.4). Until then, the boundary is asserted by
test (``tests/test_eval_guardrail.py``).
"""

from __future__ import annotations

from sqbyl.eval.benchmarks_io import Split, _read_set
from sqbyl.models import BenchmarkQuestion
from sqbyl.project import Project


def load_held_out_set(project: Project) -> list[BenchmarkQuestion]:
    """The held-out ``test.yaml`` set. Eval and humans only."""
    return _read_set(project, Split.test)


def load_for_eval(project: Project, split: Split | str) -> list[BenchmarkQuestion]:
    """Load either split **by name** — for ``eval``, which alone may read both sets."""
    return _read_set(project, Split(split))
