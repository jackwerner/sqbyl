"""Loading a sqbyl project from disk (spec §4).

A project is a plain directory: ``sqbyl.yaml`` plus ``semantics/``, ``examples/``,
``benchmarks/`` … . ``Project`` is the small handle the dev commands share for
finding the manifest and opening the (read-only) database it points at.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqbyl.models import SqbylManifest
from sqbyl.yamlio import load_yaml
from sqbyl_runtime.db import Database


@dataclass(frozen=True)
class Project:
    """A loaded sqbyl project rooted at a directory."""

    root: Path
    manifest: SqbylManifest

    @classmethod
    def load(cls, root: str | Path) -> Project:
        root = Path(root)
        manifest_path = root / "sqbyl.yaml"
        if not manifest_path.exists():
            raise FileNotFoundError(f"no sqbyl.yaml found in {root}")
        manifest = SqbylManifest.model_validate(load_yaml(manifest_path.read_text()))
        return cls(root=root, manifest=manifest)

    @property
    def semantics_dir(self) -> Path:
        return self.root / "semantics"

    def connect(self) -> Database:
        """Open the project's database, read-only by default per the manifest."""
        db = self.manifest.database
        return Database.connect(db.url, dialect=db.dialect, read_only=db.read_only)
