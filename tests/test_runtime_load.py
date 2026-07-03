"""Phase 8.2 — `sqbyl_runtime.load()` + `ask` + mismatch warnings (spec §11).

The plan's "done when": a release loads and answers under a *different* injected model with
the mismatch warning firing; an import test proves the dev machinery isn't reachable from
the runtime package. Plus: the schema-mismatch warning fires when the live DB drifts from the
release, and stays silent on the healthy, unchanged DB the release was built against.

The whole thing runs under an injected `MockLLMClient` — the runtime never spends a token in
CI (invariant 4), and `load()` exposes an `llm=` seam precisely so it can be driven this way.
"""

from __future__ import annotations

import ast
import shutil
import warnings
from pathlib import Path

import pytest

from sqbyl.eval.report import save_run
from sqbyl.models import QuestionResult, ScoredRun, Verdict
from sqbyl.project import Project
from sqbyl.projectfiles import load_knowledge, load_semantics
from sqbyl.release import build_release
from sqbyl_runtime import Agent, ModelMismatchWarning, SchemaMismatchWarning, load
from sqbyl_runtime.cost import price_usage
from sqbyl_runtime.fingerprint import fingerprint_knowledge, live_schema_fingerprint
from sqbyl_runtime.llm.base import Usage
from sqbyl_runtime.llm.mock import MockLLMClient, structured_reply
from sqbyl_runtime.models import ReleaseArtifact
from sqbyl_runtime.state.layout import SqbylPaths

_MODEL = "claude-opus-4-8"
# The one SQL the mock agent "writes" — a real read against the DuckDB fixture, so the
# pipeline's static-validate + execute path actually runs.
_ANSWER_SQL = "SELECT COUNT(*) AS n FROM analytics.orders"
# The Phase 2 cassette recorded against the dogfood project's compiled context. Because a
# release built from that same project produces the identical ProjectKnowledge (hence the
# identical prompt), `load(...).ask(...)` replays it — the record-replay fixture invariant 4
# wants for the runtime's own public LLM path, not just a mock.
_CASSETTE = Path(__file__).resolve().parent / "cassettes" / "ask_total_orders.json"


def _agent_reply() -> object:
    return structured_reply(
        {"plan": "count the orders", "sql": _ANSWER_SQL, "used_assets": []},
        usage=Usage(input_tokens=800, output_tokens=40),
    )


@pytest.fixture
def release(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> ReleaseArtifact:
    """A real release built from the dogfood project + a verified held-out run — stamped with
    the live schema fingerprint, so load() against the same DuckDB sees no drift."""
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused")
    dst = tmp_path / "proj"
    shutil.copytree(dogfood_dir, dst)
    project = Project.load(dst)
    paths = SqbylPaths(dst).ensure()
    with project.connect() as db:
        schema_fp = live_schema_fingerprint(db, load_semantics(project))
    run = ScoredRun(
        run_id="test_run",
        split="test",
        models={"agent": _MODEL},
        knowledge_fingerprint=fingerprint_knowledge(load_knowledge(project)),
        schema_fingerprint=schema_fp,
        results=[_ok("t1"), _ok("t2")],
    )
    save_run(paths, run)
    return build_release(project, "v1")


def _ok(qid: str) -> QuestionResult:
    usage = Usage(input_tokens=1000, output_tokens=200)
    return QuestionResult(
        id=qid,
        question=f"question {qid}?",
        generated_sql=_ANSWER_SQL,
        gold_sql=_ANSWER_SQL,
        verdict=Verdict.correct,
        usage=usage,
        cost_usd=price_usage(usage, _MODEL),
    )


# ── load + answer ───────────────────────────────────────────────────────────────────────


def test_load_returns_an_agent_that_answers(release: ReleaseArtifact, duckdb_path: Path) -> None:
    agent = load(release, db=str(duckdb_path), model=_MODEL, llm=MockLLMClient([_agent_reply()]))
    assert isinstance(agent, Agent)
    result = agent.ask("How many orders are there?")
    # The pipeline actually validated and executed the SQL against the injected DB.
    assert result.ok
    assert result.sql == _ANSWER_SQL
    assert result.rows and result.rows[0][0] == 2000  # the fixture has 2000 orders
    assert result.usage.total_tokens > 0
    assert result.model == _MODEL  # per-answer provenance: which model produced it
    agent.close()


def test_load_from_a_json_file(release: ReleaseArtifact, tmp_path: Path, duckdb_path: Path) -> None:
    # Loading from the on-disk artifact (the real production path) works too.
    path = tmp_path / "revenue-analytics.v1.json"
    path.write_text(release.model_dump_json())
    client = MockLLMClient([_agent_reply()])
    with load(path, db=str(duckdb_path), model=_MODEL, llm=client) as agent:
        assert agent.ask("count").rows[0][0] == 2000


def test_load_and_ask_under_record_replay(release: ReleaseArtifact, duckdb_path: Path) -> None:
    # The runtime's public load→ask surface, exercised through a real recorded cassette (not a
    # hand-scripted mock) — the record-replay round-trip invariant 4 requires for every LLM path.
    from sqbyl_runtime.llm.replay import RecordReplayLLMClient

    client = RecordReplayLLMClient(_CASSETTE, mode="replay")
    with load(release, db=str(duckdb_path), model=_MODEL, llm=client) as agent:
        result = agent.ask("How many orders are there in total?")
    assert result.ok
    assert result.rows and result.rows[0][0] == 2000


def test_load_accepts_an_already_open_database(release: ReleaseArtifact, duckdb_path: Path) -> None:
    from sqbyl_runtime.db import Database
    from sqbyl_runtime.models import Dialect

    db = Database.connect(str(duckdb_path), dialect=Dialect.duckdb, read_only=True)
    agent = load(release, db=db, model=_MODEL, llm=MockLLMClient([_agent_reply()]))
    assert agent.ask("count").rows[0][0] == 2000
    agent.close()


# ── the two non-fatal load-time checks ────────────────────────────────────────────────────


def test_model_mismatch_warns_but_still_loads_and_answers(
    release: ReleaseArtifact, duckdb_path: Path
) -> None:
    # The scorecard was blessed on opus; loading under a different model must warn (the number
    # is only meaningful for the blessed model) — but still return a working agent (spec §11).
    other = "claude-haiku-4-5-20251001"
    with pytest.warns(ModelMismatchWarning, match="blessed|earned on|meaningful"):
        agent = load(release, db=str(duckdb_path), model=other, llm=MockLLMClient([_agent_reply()]))
    assert agent.ask("count").rows[0][0] == 2000  # non-fatal: it answers under the new model
    agent.close()


def test_matching_model_does_not_warn(release: ReleaseArtifact, duckdb_path: Path) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning becomes an error
        client = MockLLMClient([_agent_reply()])
        agent = load(release, db=str(duckdb_path), model=_MODEL, llm=client)
        agent.close()


def test_schema_mismatch_warns_when_the_live_db_drifts(
    release: ReleaseArtifact, duckdb_path: Path
) -> None:
    # Point the release at a DB whose schema differs from the one it was built against: the
    # release's tables (analytics.*) don't exist here, so the fingerprint drifts → warn.
    import duckdb

    drifted = duckdb_path.parent / "drifted.duckdb"
    if drifted.exists():
        drifted.unlink()
    con = duckdb.connect(str(drifted))
    con.execute("CREATE SCHEMA analytics; CREATE TABLE analytics.orders (order_id INTEGER)")
    con.close()
    try:
        # analytics.customers is missing entirely → the warning names it (actionable, not vague).
        with pytest.warns(SchemaMismatchWarning, match="analytics.customers"):
            agent = load(
                release, db=str(drifted), model=_MODEL, llm=MockLLMClient([_agent_reply()])
            )
        agent.close()
    finally:
        drifted.unlink()


def test_unblessed_release_warns_that_accuracy_is_unattributable(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A scorecard never tied to an agent model is the *least* certain case, not a match — it
    # must warn distinctly rather than stay silent (which reads as "fine").
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))
    dst = tmp_path / "proj"
    shutil.copytree(dogfood_dir, dst)
    project = Project.load(dst)
    paths = SqbylPaths(dst).ensure()
    with project.connect() as db:
        schema_fp = live_schema_fingerprint(db, load_semantics(project))
    save_run(
        paths,
        ScoredRun(
            run_id="unblessed",
            split="test",
            models={},  # nothing blessed
            knowledge_fingerprint=fingerprint_knowledge(load_knowledge(project)),
            schema_fingerprint=schema_fp,
            results=[_ok("t1")],
        ),
    )
    release = build_release(project, "v1")
    with pytest.warns(ModelMismatchWarning, match="isn't tied to any blessed agent model"):
        agent = load(
            release, db=str(duckdb_path), model=_MODEL, llm=MockLLMClient([_agent_reply()])
        )
    agent.close()


def test_healthy_db_does_not_warn_about_schema(release: ReleaseArtifact, duckdb_path: Path) -> None:
    # The whole point of computing the fingerprint from the live inspector on both sides: the
    # unchanged DuckDB the release was built against must NOT false-positive (text vs varchar).
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        client = MockLLMClient([_agent_reply()])
        agent = load(release, db=str(duckdb_path), model=_MODEL, llm=client)
        agent.close()


def test_warn_false_silences_the_drift_warnings(
    release: ReleaseArtifact, duckdb_path: Path
) -> None:
    # warn=False silences the *advisory drift* warnings (an intentional, vetted model swap).
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        client = MockLLMClient([_agent_reply()])
        agent = load(release, db=str(duckdb_path), model="some-other-model", llm=client, warn=False)
        agent.close()


def test_warn_false_does_not_silence_the_privilege_safety_warning(
    release: ReleaseArtifact, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The write-capable-credential warning is a data-safety control of a different class — it
    # must NOT be silenceable via the advisory-drift flag. Assert load always connects warn=True.
    captured: dict[str, object] = {}
    real_connect = __import__("sqbyl_runtime.db", fromlist=["Database"]).Database.connect

    def _spy(url: str, **kw: object) -> object:
        captured.update(kw)
        return real_connect(url, **kw)

    monkeypatch.setattr("sqbyl_runtime.runtime.Database.connect", staticmethod(_spy))
    agent = load(
        release, db=str(duckdb_path), model=_MODEL, llm=MockLLMClient([_agent_reply()]), warn=False
    )
    assert captured["warn"] is True  # privilege check stays on despite warn=False
    agent.close()


# ── the package boundary: dev machinery is not reachable from the runtime ──────────────────


def _is_dev(name: str) -> bool:
    return name == "sqbyl" or name.startswith("sqbyl.")


def test_runtime_never_imports_the_dev_toolkit() -> None:
    """A fast, explicit source scan (the plan's done-when): no module under `sqbyl_runtime` may
    import `sqbyl`, statically **or** dynamically (`__import__("sqbyl…")`,
    `importlib.import_module("sqbyl…")`).

    This is a smoke check, not the authority: the import-linter ``forbidden`` contract in
    ``pyproject.toml`` is what enforces the boundary transitively in CI. This just makes an
    obvious regression fail a unit test too. Relative imports can't cross top-level packages,
    so they need no handling here."""
    root = Path(__import__("sqbyl_runtime").__file__).parent
    offenders: list[str] = []
    for py in root.rglob("*.py"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            elif isinstance(node, ast.Call):
                # `__import__("sqbyl…")` / `importlib.import_module("sqbyl…")` — first str arg.
                fn = node.func
                is_dynamic = (isinstance(fn, ast.Name) and fn.id == "__import__") or (
                    isinstance(fn, ast.Attribute) and fn.attr == "import_module"
                )
                if is_dynamic and node.args and isinstance(node.args[0], ast.Constant):
                    names = [str(node.args[0].value)]
            if any(_is_dev(n) for n in names):
                offenders.append(f"{py.relative_to(root)}: {names}")
    assert not offenders, f"sqbyl_runtime must not import the dev toolkit:\n{offenders}"
