"""Reading and writing ``semantics/*.yaml`` while respecting hand edits (spec §4).

Two subtleties this module exists to handle:

* **The ``profile: false`` opt-out.** A human sets ``profile: false`` on a column or
  a whole table to keep PII out of the project files (spec §13). This is a
  pydantic-owned shape (``Column.profile`` / ``TableSemantics.profile`` accept
  ``False``), so detection is model-driven — no magic-literal dict checks. When
  writing profile blocks back we still merge into the *raw* YAML so the opt-out
  marker and any human descriptions survive byte-for-byte (a model round-trip with
  ``exclude_defaults`` would reshuffle hand-authored files).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqbyl.profile import ProfileOptions
from sqbyl.yamlio import dump_yaml, load_yaml
from sqbyl_runtime.models import Profile, TableSemantics


def table_filename(qualified_table: str) -> str:
    """``analytics.orders`` -> ``orders.yaml`` (the unqualified name)."""
    return f"{qualified_table.rsplit('.', 1)[-1]}.yaml"


def write_draft(table: TableSemantics, path: Path) -> None:
    """Write a freshly introspected (profile-less) draft, dropping empty/default fields."""
    data = table.model_dump(exclude_none=True, exclude_defaults=True)
    path.write_text(dump_yaml(data))


def dump_yaml_path(data: dict[str, Any], path: Path) -> None:
    """Write a merged raw-YAML dict back to a semantics file."""
    path.write_text(dump_yaml(data))


@dataclass(frozen=True)
class LoadedSemantics:
    """A semantics file parsed for profiling: the validated model, the raw dict to
    merge results back into, the PII opt-out, and whether the whole table is opted out."""

    table: TableSemantics
    raw: dict[str, Any]
    options: ProfileOptions
    table_skipped: bool


def load_for_profiling(path: Path) -> LoadedSemantics:
    raw = load_yaml(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path} is not a mapping")

    # The `profile: false` opt-out is part of the model, so validate directly and
    # read the opt-out off the validated shape — no pre-parse dict surgery.
    table = TableSemantics.model_validate(raw)
    skip = {c.name for c in table.columns if c.profile is False}
    return LoadedSemantics(
        table=table,
        raw=raw,
        options=ProfileOptions(skip=skip),
        table_skipped=table.profile is False,
    )


def merge_profiles(loaded: LoadedSemantics, profiled: TableSemantics) -> dict[str, Any]:
    """Overlay computed ``profile``/``sample_values`` onto the raw YAML dict.

    Skipped columns (``profile: false``) and every human-authored key are preserved
    exactly; only profiled columns gain/refresh their stats.
    """
    raw = dict(loaded.raw)
    by_name = {c.name: c for c in profiled.columns}
    out_columns = []
    for col in raw.get("columns", []):
        col = dict(col)
        name = col.get("name")
        if name in loaded.options.skip or name not in by_name:
            out_columns.append(col)
            continue
        computed = by_name[name]
        if isinstance(computed.profile, Profile):
            col["profile"] = _profile_dict(computed.profile)
        if computed.sample_values is not None:
            col["sample_values"] = list(computed.sample_values)
        out_columns.append(col)
    raw["columns"] = out_columns
    return raw


def _profile_dict(profile: Profile) -> dict[str, Any]:
    # Drop None fields and the default `sampled: false` for compact, readable blocks.
    return profile.model_dump(exclude_none=True, exclude_defaults=True)
