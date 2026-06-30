"""Phase 0.1 — prove the workspace, both packages, and the entry point import."""

from __future__ import annotations


def test_packages_import() -> None:
    import sqbyl
    import sqbyl_runtime

    assert sqbyl.__version__
    assert sqbyl_runtime.__version__


def test_cli_version(capsys) -> None:  # type: ignore[no-untyped-def]
    from sqbyl.cli import main

    rc = main(["--version"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "sqbyl" in out
