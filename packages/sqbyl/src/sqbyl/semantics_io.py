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

from sqbyl.annotate import TableAnnotation
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


def sync_new_columns(
    raw: dict[str, Any], live: TableSemantics
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Overlay the *live* schema's column list onto a hand-authored file, **additively**.

    Schema drift (a new DB column) is otherwise a lose-lose: ``profile`` never discovers a
    column that isn't already in the YAML, and ``introspect --force`` rewrites the whole file,
    discarding every description/synonym/profile on it (finding #12). This appends any column
    the live table has but the file lacks — as a draft row (name + type + DB comment), profile-
    less, exactly like a fresh introspect — while leaving every existing column (and all its
    hand-authored meaning) byte-for-byte untouched.

    Returns ``(merged_raw, added, removed)``: ``added`` are the appended column names; ``removed``
    are columns still in the file but gone from the live schema (reported, never deleted — a
    human decides whether a dropped column should leave the semantics)."""
    existing = [c for c in raw.get("columns", []) if isinstance(c, dict)]
    existing_names = {c.get("name") for c in existing}
    live_by_name = {c.name: c for c in live.columns}

    added = [c.name for c in live.columns if c.name not in existing_names]
    removed = [n for n in existing_names if isinstance(n, str) and n not in live_by_name]

    out = dict(raw)
    columns = [dict(c) for c in existing]
    for name in added:
        col = live_by_name[name]
        draft: dict[str, Any] = {"name": col.name, "type": col.type}
        if col.description:  # carry a DB comment through, same as a fresh introspect draft
            draft["description"] = col.description
        columns.append(draft)
    out["columns"] = columns
    return out, added, removed


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


def merge_annotation(raw: dict[str, Any], annotation: TableAnnotation) -> dict[str, Any]:
    """Overlay drafted descriptions/synonyms onto the raw YAML dict — **fill-only**.

    Like ``merge_profiles``, this writes into the raw mapping so profile blocks,
    ``profile: false`` markers, and key order survive. Two honesty rules (finding B11):

    * A **non-empty existing description is never overwritten** — it's authoritative (a DB
      catalog comment carried through by ``introspect``, or a human edit). The draft only
      fills a blank slot; a contested/uncertain draft is withheld upstream (see
      ``reconcile_annotation``) so it arrives here already blanked.
    * **Synonyms are additive** — the draft's are unioned onto any existing ones, never
      replacing them.
    """
    out = dict(raw)
    filled = _fill_description(out.get("description"), annotation.description)
    if filled is not None:
        out["description"] = filled
    merged_syn = _union_synonyms(out.get("synonyms"), annotation.synonyms)
    if merged_syn:
        out["synonyms"] = merged_syn
    by_name = {c.name: c for c in annotation.columns}
    columns = []
    for col in out.get("columns", []):
        col = dict(col)
        name = col.get("name")
        drafted = by_name.get(name) if isinstance(name, str) else None
        if drafted is not None:
            filled = _fill_description(col.get("description"), drafted.description)
            if filled is not None:
                col["description"] = filled
            merged = _union_synonyms(col.get("synonyms"), drafted.synonyms)
            if merged:
                col["synonyms"] = merged
        columns.append(col)
    out["columns"] = columns
    return out


def _fill_description(existing: object, draft: str) -> str | None:
    """Keep a non-empty existing description; only a blank slot is filled with the draft.
    Returns ``None`` when there's nothing to write (so the key is left untouched)."""
    if isinstance(existing, str) and existing.strip():
        return None  # authoritative — never overwrite
    return draft.strip() or None


def _union_synonyms(existing: object, drafted: list[str]) -> list[str]:
    """Additive union preserving order (existing first), de-duplicated case-insensitively."""
    out: list[str] = []
    seen: set[str] = set()
    source = (list(existing) if isinstance(existing, list) else []) + list(drafted)
    for s in source:
        key = s.lower() if isinstance(s, str) else str(s)
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out
