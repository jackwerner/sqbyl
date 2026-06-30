"""Phase 1 CLI surface — the free `$0` pass: `sqbyl introspect` then `sqbyl profile`.

Exit criteria: `sqbyl introspect` writes draft semantics/*.yaml; `sqbyl profile`
writes profile: blocks; a `profile: false` opt-out and human edits survive a re-run.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from sqbyl.cli import main
from sqbyl.yamlio import dump_yaml, load_yaml


@pytest.fixture
def project(tmp_path: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))
    (tmp_path / "sqbyl.yaml").write_text(
        textwrap.dedent(
            """
            name: cli-smoke
            database:
              dialect: duckdb
              url: env:DATABASE_URL
              read_only: true
            model:
              provider: anthropic
              api_key: env:ANTHROPIC_API_KEY
              default: claude-opus-4-8
            """
        ).strip()
        + "\n"
    )
    return tmp_path


def test_introspect_writes_drafts(project: Path) -> None:
    assert main(["introspect", str(project)]) == 0
    sem = project / "semantics"
    assert (sem / "orders.yaml").exists()
    assert (sem / "customers.yaml").exists()
    orders = load_yaml((sem / "orders.yaml").read_text())
    assert orders["table"] == "analytics.orders"
    assert orders["joins"][0]["to"] == "analytics.customers"
    # No profiling yet at the introspect stage.
    assert all("profile" not in c for c in orders["columns"])


def test_introspect_does_not_clobber_without_force(project: Path) -> None:
    assert main(["introspect", str(project)]) == 0
    path = project / "semantics" / "orders.yaml"
    path.write_text(path.read_text() + "\n# human edit\n")
    before = path.read_text()
    assert main(["introspect", str(project)]) == 0  # no --force
    assert path.read_text() == before  # untouched
    assert main(["introspect", str(project), "--force"]) == 0
    assert "# human edit" not in path.read_text()  # overwritten


def test_profile_writes_blocks(project: Path) -> None:
    assert main(["introspect", str(project)]) == 0
    assert main(["profile", str(project)]) == 0
    orders = load_yaml((project / "semantics" / "orders.yaml").read_text())
    status = next(c for c in orders["columns"] if c["name"] == "status")
    assert status["profile"]["distinct"] == 3
    assert status["sample_values"] == ["confirmed", "partial_refund", "refunded"]


def test_profile_false_opt_out_and_edits_survive(project: Path) -> None:
    assert main(["introspect", str(project)]) == 0
    path = project / "semantics" / "customers.yaml"
    raw = load_yaml(path.read_text())
    for col in raw["columns"]:
        if col["name"] == "email":
            col["profile"] = False  # PII opt-out
        if col["name"] == "region":
            col["description"] = "Sales region (human-authored)."
    path.write_text(dump_yaml(raw))

    assert main(["profile", str(project)]) == 0

    after = load_yaml(path.read_text())
    by_name = {c["name"]: c for c in after["columns"]}
    # The opt-out marker survives and email was never profiled.
    assert by_name["email"]["profile"] is False
    assert "sample_values" not in by_name["email"]
    # The human description survives the profile write-back.
    assert by_name["region"]["description"] == "Sales region (human-authored)."
    assert by_name["region"]["profile"]["distinct"] == 4
