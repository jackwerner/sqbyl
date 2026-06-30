"""Thin CLI wrapper over the Python API.

The Python API (``sqbyl.Project`` + the introspect/profile functions) is the
substrate; this CLI is a thin shell over it. Commands are added phase by phase.

Phase 1 surfaces the free, deterministic "$0" pass (spec §5.5): ``introspect`` and
``profile`` cost no tokens and run no LLM, so they print that framing.
"""

from __future__ import annotations

import sys

from sqbyl import __version__


def _schema_export(args: list[str]) -> int:
    """`sqbyl schema export` — regenerate the checked-in release JSON Schema."""
    from sqbyl_runtime.schema import schema_text, write_release_schema

    if "--stdout" in args:
        sys.stdout.write(schema_text())
        return 0
    path = write_release_schema()
    print(f"wrote {path}")
    return 0


def _introspect(args: list[str]) -> int:
    """`sqbyl introspect [DIR] [--force]` — draft semantics/*.yaml from the live schema."""
    from sqbyl.introspect import introspect
    from sqbyl.project import Project
    from sqbyl.semantics_io import table_filename, write_draft

    force = "--force" in args
    positional = [a for a in args if not a.startswith("-")]
    project = Project.load(positional[0] if positional else ".")

    print("▸ introspecting schema (read-only SQL)…  ($0 — no LLM)")
    project.semantics_dir.mkdir(parents=True, exist_ok=True)
    with project.connect() as db:
        tables = introspect(db)

    wrote, skipped = 0, 0
    for table in tables:
        path = project.semantics_dir / table_filename(table.table)
        if path.exists() and not force:
            print(f"  · {path.name} exists — skipping (use --force to overwrite)")
            skipped += 1
            continue
        write_draft(table, path)
        print(f"  ✓ {path.name}  ({len(table.columns)} columns, {len(table.joins)} joins)")
        wrote += 1
    print(f"done — wrote {wrote}, skipped {skipped}")
    return 0


def _profile(args: list[str]) -> int:
    """`sqbyl profile [DIR]` — write deterministic profile: blocks into the semantics."""
    from sqbyl.profile import profile_table
    from sqbyl.project import Project
    from sqbyl.semantics_io import dump_yaml_path, load_for_profiling, merge_profiles

    positional = [a for a in args if not a.startswith("-")]
    project = Project.load(positional[0] if positional else ".")
    paths = sorted(project.semantics_dir.glob("*.yaml"))
    if not paths:
        print("no semantics/*.yaml found — run `sqbyl introspect` first")
        return 1

    print("▸ profiling columns (read-only SQL)…  ($0 — no LLM)")
    with project.connect() as db:
        for path in paths:
            loaded = load_for_profiling(path)
            if loaded.table_skipped:
                print(f"  · {path.name} opted out (profile: false) — skipping")
                continue
            profiled = profile_table(db, loaded.table, options=loaded.options)
            merged = merge_profiles(loaded, profiled)
            dump_yaml_path(merged, path)
            sampled = any(c.profile and c.profile.sampled for c in profiled.columns)
            note = " (sampled)" if sampled else ""
            print(f"  ✓ {path.name}{note}")
    print("done")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in {"-V", "--version", "version"}:
        print(f"sqbyl {__version__}")
        return 0
    if len(args) >= 2 and args[0] == "schema" and args[1] == "export":
        return _schema_export(args[2:])
    if args and args[0] == "introspect":
        return _introspect(args[1:])
    if args and args[0] == "profile":
        return _profile(args[1:])
    print("sqbyl: commands — introspect, profile, schema export, version")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
