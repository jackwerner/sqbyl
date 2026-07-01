"""The synth → review candidate queue on disk (``.sqbyl/candidates.yaml``).

``sqbyl synth`` writes survivors here; the review console reads them, shows the executed
evidence, and on accept promotes a candidate into ``benchmarks/dev.yaml`` (via
:func:`sqbyl.eval.benchmarks_io.append_to_dev_set`) and marks it accepted. The queue is
``.sqbyl/`` scratch state — regenerable, gitignored — never a source of truth for the
golden set itself.

This module is deliberately **dev-safe**: it reads and writes only the candidate queue and
never touches ``test.yaml`` (invariant 3).
"""

from __future__ import annotations

from pathlib import Path

from sqbyl.models import Candidate
from sqbyl.project import Project
from sqbyl.yamlio import dump_yaml, load_yaml
from sqbyl_runtime.state.layout import SqbylPaths


def candidates_path(project: Project) -> Path:
    return SqbylPaths(project.root).root / "candidates.yaml"


def load_candidates(project: Project) -> list[Candidate]:
    path = candidates_path(project)
    if not path.exists():
        return []
    raw = load_yaml(path.read_text()) or []
    return [Candidate.model_validate(item) for item in raw]


def save_candidates(project: Project, candidates: list[Candidate]) -> Path:
    """Overwrite the queue with ``candidates`` (JSON-native scalars for portability)."""
    path = candidates_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [c.model_dump(mode="json") for c in candidates]
    path.write_text(dump_yaml(data))
    return path


def add_candidates(project: Project, new: list[Candidate]) -> list[Candidate]:
    """Append newly synthesized candidates to the queue, replacing any with the same id.

    Returns the full queue after the merge. New candidates win on an id collision so a
    re-synth refreshes stale pending items rather than duplicating them.
    """
    by_id = {c.id: c for c in load_candidates(project)}
    for candidate in new:
        by_id[candidate.id] = candidate
    merged = list(by_id.values())
    save_candidates(project, merged)
    return merged


def get_candidate(project: Project, candidate_id: str) -> Candidate | None:
    return next((c for c in load_candidates(project) if c.id == candidate_id), None)


def update_candidate(project: Project, candidate: Candidate) -> None:
    """Persist an updated candidate (matched by id) back into the queue."""
    candidates = load_candidates(project)
    replaced = [candidate if c.id == candidate.id else c for c in candidates]
    save_candidates(project, replaced)
