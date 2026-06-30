"""Load a dev project's knowledge off disk into a runtime ``ProjectKnowledge``.

The runtime pipeline reasons over a ``ProjectKnowledge`` (semantics + examples +
trusted assets + instructions). At dev time that bundle is assembled from the plain
project files; in production it comes from the release. This module owns the dev
side, including parsing the ``trusted/*.sql`` header convention into ``TrustedAsset``
models (spec §4).
"""

from __future__ import annotations

import re

from sqbyl.project import Project
from sqbyl.yamlio import load_yaml
from sqbyl_runtime.context import ProjectKnowledge
from sqbyl_runtime.models import AssetParam, Example, TableSemantics, TrustedAsset

_HEADER_RE = re.compile(r"^--\s*@(name|params|description):\s*(.*)$")
_PARAM_RE = re.compile(r"(\w+)\s*\(\s*(\w+)\s*\)")


def load_semantics(project: Project) -> list[TableSemantics]:
    return [
        TableSemantics.model_validate(load_yaml(path.read_text()))
        for path in sorted(project.semantics_dir.glob("*.yaml"))
    ]


def load_examples(project: Project) -> list[Example]:
    path = project.root / "examples"
    examples: list[Example] = []
    for file in sorted(path.glob("*.yaml")):
        raw = load_yaml(file.read_text()) or []
        examples.extend(Example.model_validate(item) for item in raw)
    return examples


def parse_trusted_sql(text: str) -> TrustedAsset:
    """Parse a ``trusted/*.sql`` file with ``-- @name/@params/@description`` headers."""
    name: str | None = None
    description: str | None = None
    params: list[AssetParam] = []
    body: list[str] = []
    for line in text.splitlines():
        header = _HEADER_RE.match(line.strip())
        if header is None:
            # A non-header comment line is dropped; everything else is SQL body.
            if not line.strip().startswith("--"):
                body.append(line)
            continue
        key, value = header.group(1), header.group(2).strip()
        if key == "name":
            name = value
        elif key == "description":
            description = value
        elif key == "params":
            params = [
                AssetParam(name=m.group(1), type=m.group(2)) for m in _PARAM_RE.finditer(value)
            ]
    if not name:
        raise ValueError("trusted asset is missing a `-- @name:` header")
    return TrustedAsset(
        name=name, description=description, params=params, sql="\n".join(body).strip()
    )


def load_trusted_assets(project: Project) -> list[TrustedAsset]:
    path = project.root / "trusted"
    return [parse_trusted_sql(file.read_text()) for file in sorted(path.glob("*.sql"))]


def load_instructions(project: Project) -> str:
    path = project.root / "instructions.md"
    return path.read_text() if path.exists() else ""


def load_knowledge(project: Project) -> ProjectKnowledge:
    """Assemble the full ``ProjectKnowledge`` the runtime pipeline reasons over."""
    return ProjectKnowledge(
        dialect=project.manifest.database.dialect,
        semantics=load_semantics(project),
        instructions=load_instructions(project),
        examples=load_examples(project),
        trusted_assets=load_trusted_assets(project),
    )
