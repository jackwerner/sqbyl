"""Thin CLI wrapper over the Python API.

The Python API (`sqbyl.Project`) is the substrate; this CLI is a thin shell over
it. Commands are added phase by phase; for now this is a stub that proves the
entry point is wired up.
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


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in {"-V", "--version", "version"}:
        print(f"sqbyl {__version__}")
        return 0
    if len(args) >= 2 and args[0] == "schema" and args[1] == "export":
        return _schema_export(args[2:])
    print("sqbyl: no commands wired up yet (pre-code scaffold).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
