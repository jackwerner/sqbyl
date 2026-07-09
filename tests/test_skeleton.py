"""Phase 0.1 — prove the workspace, both packages, and the entry point import."""

from __future__ import annotations


def test_packages_report_real_installed_version() -> None:
    # __version__ resolves from installed package metadata, not a hardcoded "0.0.0"
    # placeholder — so `sqbyl --version` and `sqbyl_runtime.__version__` are truthful.
    from importlib.metadata import version

    import sqbyl
    import sqbyl_runtime

    assert sqbyl.__version__ == version("sqbyl") != "0.0.0"
    assert sqbyl_runtime.__version__ == version("sqbyl-runtime") != "0.0.0"
    # The two packages are cut in lockstep, so their versions match.
    assert sqbyl.__version__ == sqbyl_runtime.__version__


def test_cli_version(capsys) -> None:  # type: ignore[no-untyped-def]
    import sqbyl
    from sqbyl.cli import main

    rc = main(["--version"])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"sqbyl {sqbyl.__version__}" in out
