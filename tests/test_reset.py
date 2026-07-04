"""`sqbyl reset` — clear local `.sqbyl/` state for a clean slate (sqbyl-enhancements.md §4.1).

Default clears derived scratch but keeps the two audit trails (cost history + judge
calibration); `--all` wipes everything; confirmation is required unless `--yes`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sqbyl.cli import main

_DERIVED = ("runs", "traces", "coach", "enrichment.json", "candidates.yaml", "feedback.jsonl")
_AUDIT = ("usage.db", "calibration.jsonl")


def _seed(root: Path) -> Path:
    d = root / ".sqbyl"
    for sub in ("runs", "traces", "coach"):
        (d / sub).mkdir(parents=True)
    (d / "runs" / "r1.json").write_text("{}")
    for f in ("enrichment.json", "candidates.yaml", "feedback.jsonl", *_AUDIT):
        (d / f).write_text("x")
    return d


def test_default_reset_clears_derived_keeps_audit_trails(tmp_path: Path) -> None:
    d = _seed(tmp_path)
    assert main(["reset", str(tmp_path), "--yes"]) == 0
    for name in _AUDIT:
        assert (d / name).exists(), f"{name} should be preserved"
    for name in _DERIVED:
        assert not (d / name).exists(), f"{name} should be cleared"


def test_all_removes_the_whole_sqbyl_dir(tmp_path: Path) -> None:
    d = _seed(tmp_path)
    assert main(["reset", str(tmp_path), "--all", "--yes"]) == 0
    assert not d.exists()


def test_reset_on_missing_sqbyl_is_a_noop(tmp_path: Path) -> None:
    assert main(["reset", str(tmp_path), "--yes"]) == 0


def test_reset_with_only_audit_trails_is_a_noop(tmp_path: Path) -> None:
    d = tmp_path / ".sqbyl"
    d.mkdir()
    for f in _AUDIT:
        (d / f).write_text("x")
    assert main(["reset", str(tmp_path), "--yes"]) == 0
    for f in _AUDIT:
        assert (d / f).exists()


def test_reset_aborts_without_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _seed(tmp_path)
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert main(["reset", str(tmp_path)]) == 0
    # Declining the prompt removes nothing.
    assert (d / "runs").exists()
    assert (d / "usage.db").exists()
