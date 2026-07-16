"""Phase 2 CLI surface — `sqbyl ask` and `sqbyl annotate`.

Both are paid commands: they print an up-front estimate and meter every call to
`.sqbyl/usage.db` (invariant 5). Tested with no key via a replay cassette (`ask`)
and an injected mock client (`annotate`).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sqbyl.cli import main
from sqbyl.yamlio import load_yaml
from sqbyl_runtime.llm.mock import MockLLMClient, structured_reply
from sqbyl_runtime.state.layout import SqbylPaths
from sqbyl_runtime.state.traces import read_spans
from sqbyl_runtime.state.usage import UsageStore

_CASSETTE = Path(__file__).resolve().parent / "cassettes" / "ask_total_orders.json"


@pytest.fixture
def project(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused-in-replay")
    dst = tmp_path / "proj"
    shutil.copytree(dogfood_dir, dst)
    return dst


def _usage_rows(project: Path) -> list[object]:
    with UsageStore(SqbylPaths(project).usage_db) as store:
        return list(store.all())


def test_ask_cli_replays_and_meters(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["ask", "How many orders are there in total?", str(project), "--replay", str(_CASSETTE)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "estimated ~$" in out  # up-front estimate printed
    assert "2000" in out  # the answer
    rows = _usage_rows(project)
    assert len(rows) == 1
    assert rows[0].command == "ask"  # type: ignore[attr-defined]
    assert rows[0].cost_usd and rows[0].cost_usd > 0  # type: ignore[attr-defined]


def test_positionals_strips_space_separated_budget() -> None:
    from sqbyl.cli import _positionals

    # Regression: `--budget 1` (space form) must not survive as a positional.
    assert _positionals(["a question", "--budget", "1"]) == ["a question"]
    # Consumed option values (e.g. --replay) are dropped alongside it.
    from sqbyl.cli import _opt

    args = ["dev", "--replay", "cassette.json", "--budget", "$5"]
    assert _positionals(args, {_opt(args, "replay")}) == ["dev"]
    # An explicitly-given DIR is preserved.
    assert _positionals(["q", "./proj", "--budget", "2.50"]) == ["q", "./proj"]


def test_ask_budget_flag_is_not_read_as_project_dir(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Regression: with DIR omitted, `--budget 1` used to leak "1" into positional[1],
    # so `ask` did `Project.load("1")` → FileNotFoundError. Dry-run keeps it $0.
    monkeypatch.chdir(project)
    code = main(["ask", "How many orders are there?", "--budget", "1", "--dry-run"])
    assert code == 0
    assert "dry run" in capsys.readouterr().out.lower()


def test_annotate_cli_writes_descriptions_and_meters(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Inject a scripted client (one reply per table, sorted: customers, orders).
    def _reply(desc: str) -> object:
        return structured_reply(
            {"description": desc, "synonyms": [], "confidence": 0.9, "columns": []}
        )

    mock = MockLLMClient([_reply("A customer."), _reply("An order.")])
    monkeypatch.setattr("sqbyl.llm.build_llm_client", lambda *a, **k: mock)

    # The dogfood project is already authored, so its descriptions are authoritative.
    original_desc = load_yaml((project / "semantics" / "orders.yaml").read_text())["description"]
    assert original_desc and original_desc != "An order."

    code = main(["annotate", str(project)])
    assert code == 0
    out = capsys.readouterr().out
    assert "estimated ~$" in out

    # Honesty rule (finding B11): an existing authoritative description is never overwritten —
    # the draft "An order." is dropped, the human's text survives. Profile blocks preserved.
    orders = load_yaml((project / "semantics" / "orders.yaml").read_text())
    assert orders["description"] == original_desc  # not clobbered by the draft
    status = next(c for c in orders["columns"] if c["name"] == "status")
    assert status["profile"]["distinct"] == 3  # untouched by annotate

    # Both tables metered.
    rows = _usage_rows(project)
    assert len(rows) == 2
    assert all(r.command == "annotate" for r in rows)  # type: ignore[attr-defined]

    # The token-spending calls were traced, OTel-GenAI-shaped (invariant 7).
    spans = read_spans(SqbylPaths(project).traces_dir / "annotate.jsonl")
    run = next(s for s in spans if s.name == "annotate")
    llm_spans = [s for s in spans if s.name.startswith("annotate analytics.")]
    assert len(llm_spans) == 2
    assert all(s.parent_span_id == run.span_id for s in llm_spans)
    assert all("gen_ai.usage.input_tokens" in s.attributes for s in llm_spans)


def test_annotate_cli_budget_caps_the_loop(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Realistic per-call usage so actual spend ≈ the estimate the live cap reserves
    # against (a table costs ~$0.05 on opus); a mid-range budget admits exactly one.
    from sqbyl_runtime.llm.base import Usage

    usage = Usage(input_tokens=1500, output_tokens=400)
    mock = MockLLMClient(
        [
            structured_reply(
                {"description": d, "synonyms": [], "confidence": 0.9, "columns": []}, usage=usage
            )
            for d in ("c", "o")
        ]
    )
    monkeypatch.setattr("sqbyl.llm.build_llm_client", lambda *a, **k: mock)

    # --auto hard-stops at the cap (no prompt); ~$0.08 admits the first table (~$0.05)
    # then the pre-dispatch guard refuses the second (~$0.05 more would exceed).
    code = main(["annotate", str(project), "--auto", "--budget", "0.08"])
    assert code == 0
    out = capsys.readouterr().out
    assert "budget $0.08 reached" in out
    assert mock.call_count == 1  # the loop stopped; the second table never ran
    assert len(_usage_rows(project)) == 1
