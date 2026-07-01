"""Phase 3.4 — the dev/test boundary is structural, not a convention (invariant 3).

The held-out ``test.yaml`` must be unreachable by anything but ``eval`` and humans. The
guarantees asserted here:

1. ``load_dev_set`` is hard-wired to the dev split — it has no ``split`` parameter, so a
   coach/synth/optimize code path calling it *cannot* receive ``test.yaml``.
2. The dev-safe module (``benchmarks_io``) exposes no way to read the held-out set; that
   loader lives only in ``sqbyl.eval.heldout``.
3. The two splits are genuinely disjoint in the dogfood project, so "read dev" can never
   accidentally surface a held-out question.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import sqbyl.eval.benchmarks_io as benchmarks_io
from sqbyl.eval.benchmarks_io import Split, load_dev_set
from sqbyl.eval.heldout import load_for_eval, load_held_out_set
from sqbyl.project import Project


def test_load_dev_set_has_no_split_parameter() -> None:
    # The structural boundary: there is no argument through which test.yaml could be
    # smuggled into a dev-only consumer (coach/synth/optimize).
    params = list(inspect.signature(load_dev_set).parameters)
    assert params == ["project"]


def test_dev_safe_module_cannot_read_the_held_out_set() -> None:
    # The safe loader module exposes no held-out door and no public split-taking reader;
    # test.yaml is reachable only via sqbyl.eval.heldout (private `_read_set`), which
    # coach/synth/optimize must not import.
    assert not hasattr(benchmarks_io, "load_held_out_set")
    assert not hasattr(benchmarks_io, "load_for_eval")
    assert not hasattr(benchmarks_io, "read_set")  # only the private _read_set exists


def test_load_dev_set_returns_only_dev_questions(dogfood_dir: Path) -> None:
    project = Project.load(dogfood_dir)
    dev = load_dev_set(project)
    held_out = load_held_out_set(project)
    dev_ids = {q.id for q in dev}
    held_out_ids = {q.id for q in held_out}
    assert dev_ids  # non-empty
    assert held_out_ids
    # The dev loader never yields a held-out question.
    assert dev_ids.isdisjoint(held_out_ids)


def test_eval_door_can_read_both_splits(dogfood_dir: Path) -> None:
    project = Project.load(dogfood_dir)
    assert load_for_eval(project, Split.dev)
    assert load_for_eval(project, "test")  # accepts the split by name too


def test_dev_writer_is_hard_wired_to_dev(dogfood_dir: Path) -> None:
    # The only benchmark writer takes no `split`, so synth/console cannot write test.yaml
    # (invariant 3). The read-side boundary above has a matching write-side guarantee.
    from sqbyl.eval.benchmarks_io import append_to_dev_set

    params = list(inspect.signature(append_to_dev_set).parameters)
    assert params == ["project", "questions"]


def test_synth_module_does_not_import_the_held_out_door() -> None:
    # The dev loop (synth) must not even import `sqbyl.eval.heldout` — the same rule the
    # import-linter `forbidden` contract enforces, asserted here at the AST level too
    # (docstrings may mention the module by name; only real imports are a violation).
    import ast

    import sqbyl.synth

    tree = ast.parse(inspect.getsource(sqbyl.synth))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert "sqbyl.eval.heldout" not in imported
