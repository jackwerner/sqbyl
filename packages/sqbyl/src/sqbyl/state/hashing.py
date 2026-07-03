"""Project content-hashing (spec §3 #7, Phase 0.5).

A stable sha256 over all tracked project files, so every run links to the exact
config that produced it and ``init``/``eval`` can later re-orchestrate *only what
changed* (Phase 7). The hash is order-independent and ignores ``.sqbyl/`` state and
other non-config noise, so it changes when and only when the project content does.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

# Files/dirs that define the project's behavior. Globs are relative to the root.
_TRACKED_GLOBS = (
    "sqbyl.yaml",
    "instructions.md",
    "semantics/**/*.yaml",
    "examples/**/*.yaml",
    "trusted/**/*.sql",
    "benchmarks/**/*.yaml",
)


def tracked_files(project_root: str | Path) -> list[Path]:
    """All config files that contribute to the content hash, sorted deterministically."""
    root = Path(project_root)
    found: set[Path] = set()
    for pattern in _TRACKED_GLOBS:
        if "*" in pattern:
            found.update(p for p in root.glob(pattern) if p.is_file())
        else:
            candidate = root / pattern
            if candidate.is_file():
                found.add(candidate)
    return sorted(found)


def _hash_files(root: Path, files: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        # Hash the relative path (POSIX-normalized) then the bytes, so a rename or
        # a content change both move the hash, but cwd/OS never do.
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def content_hash(project_root: str | Path) -> str:
    """Stable content hash of the whole project (``sha256:...``)."""
    root = Path(project_root)
    return _hash_files(root, tracked_files(root))


def file_digest(path: str | Path) -> str:
    """Content hash of a single file (``sha256:...``), path-independent.

    Used by ``sqbyl init`` to re-orchestrate *only what changed* (spec §5.5, Phase 7.2): a
    semantics file's digest is recorded when it's annotated, so a later re-run re-annotates
    a table when — and only when — its file content moved. Unlike :func:`content_hash` this
    hashes bytes alone (not the relative path), so it's stable across a rename.
    """
    digest = hashlib.sha256()
    digest.update(Path(path).read_bytes())
    return "sha256:" + digest.hexdigest()
