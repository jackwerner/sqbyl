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

    code = main(["annotate", str(project)])
    assert code == 0
    out = capsys.readouterr().out
    assert "estimated ~$" in out

    # Descriptions landed in the YAML; profile blocks were preserved.
    orders = load_yaml((project / "semantics" / "orders.yaml").read_text())
    assert orders["description"] == "An order."
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
    mock = MockLLMClient(
        [
            structured_reply({"description": d, "synonyms": [], "confidence": 0.9, "columns": []})
            for d in ("c", "o")
        ]
    )
    monkeypatch.setattr("sqbyl.llm.build_llm_client", lambda *a, **k: mock)

    # A tiny budget lets the first table through, then stops before the second.
    code = main(["annotate", str(project), "--budget", "0.0000001"])
    assert code == 0
    out = capsys.readouterr().out
    assert "budget $0.00 reached" in out
    assert mock.call_count == 1  # the loop stopped; the second table never ran
    assert len(_usage_rows(project)) == 1
