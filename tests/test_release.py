"""Phase 8.1 — the release compiler (`sqbyl release create`, spec §11).

The plan's "done when": releasing the dogfood project emits a JSON that validates against
the generated schema and contains the correct scorecard. Plus the invariants that make a
release trustworthy: the headline number is the **held-out test** accuracy (never dev), the
brain carries the schema fingerprint and judge prompts, and building one spends nothing and
opens no database connection ($0 file operation, invariant 5/6).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from sqbyl.cli import main
from sqbyl.eval.report import save_run
from sqbyl.models import QuestionResult, ScoredRun, Verdict
from sqbyl.models.judges import ALL_JUDGES
from sqbyl.project import Project
from sqbyl.projectfiles import load_knowledge
from sqbyl.release import ReleaseError, build_release, release_filename
from sqbyl_runtime.cost import price_usage
from sqbyl_runtime.fingerprint import fingerprint_knowledge, fingerprint_semantics
from sqbyl_runtime.llm.base import Usage
from sqbyl_runtime.models import ReleaseArtifact
from sqbyl_runtime.schema import release_json_schema
from sqbyl_runtime.state.layout import SqbylPaths

_MODEL = "claude-opus-4-8"


def _brain_fp(project: Project) -> str:
    """The fingerprint of the project's current brain — what a real `eval test` run stamps,
    so the staleness guard sees a matching (verified) scorecard."""
    return fingerprint_knowledge(load_knowledge(project))


def _q(qid: str, verdict: Verdict, *, latency: float = 100.0) -> QuestionResult:
    usage = Usage(input_tokens=1000, output_tokens=200)
    return QuestionResult(
        id=qid,
        question=f"question {qid}?",
        generated_sql=f"SELECT '{qid}'",
        gold_sql=f"SELECT '{qid}'",
        verdict=verdict,
        usage=usage,
        cost_usd=price_usage(usage, _MODEL),
        latency_ms=latency,
    )


@pytest.fixture
def project(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Project:
    """A dogfood project with a held-out **test** run (3/4 correct) and a **dev** run
    (4/4 correct) already persisted — the eval history a release stamps its scorecard from."""
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused")
    dst = tmp_path / "proj"
    shutil.copytree(dogfood_dir, dst)
    project = Project.load(dst)
    paths = SqbylPaths(dst).ensure()

    fp = _brain_fp(project)
    dev = ScoredRun(
        run_id="dev_run",
        split="dev",
        models={"agent": _MODEL, "judge": _MODEL},
        knowledge_fingerprint=fp,
        results=[_q(f"d{i}", Verdict.correct) for i in range(4)],
    )
    # The held-out run is stamped with the current brain — a *verified* scorecard.
    test = ScoredRun(
        run_id="test_run",
        split="test",
        models={"agent": _MODEL},
        knowledge_fingerprint=fp,
        results=[
            _q("t1", Verdict.correct, latency=100.0),
            _q("t2", Verdict.correct, latency=200.0),
            _q("t3", Verdict.correct, latency=300.0),
            _q("t4", Verdict.incorrect, latency=400.0),
        ],
    )
    save_run(paths, dev)
    save_run(paths, test)
    return project


# ── the artifact + scorecard ────────────────────────────────────────────────────────────


def test_build_release_stamps_the_held_out_scorecard(project: Project) -> None:
    release = build_release(project, "v1")
    assert isinstance(release, ReleaseArtifact)
    assert release.name == "revenue-analytics" and release.tag == "v1"

    sc = release.scorecard
    # Headline is the held-out TEST number (3/4), never dev — the only set not optimized on.
    assert sc.benchmark == "test"
    assert sc.accuracy == pytest.approx(0.75)
    assert sc.n == 4
    # Dev sits beside it so a reviewer sees the gap (dev was 4/4).
    assert sc.dev_accuracy == pytest.approx(1.0)
    assert sc.dev_n == 4
    # An accuracy number is only meaningful for the model that produced it.
    assert sc.blessed_with_models == {"agent": _MODEL}
    # p50 latency of 100/200/300/400 is 250.
    assert sc.latency_p50_ms == pytest.approx(250.0)
    # The point estimate carries its Wilson interval and its unresolved count — a bare
    # percentage over-states a tens-of-questions number (ml-systems).
    assert sc.accuracy_low is not None and sc.accuracy_high is not None
    assert sc.accuracy_low < sc.accuracy < sc.accuracy_high
    assert sc.n_manual_review == 0  # this run's only miss is a hard incorrect, not unresolved
    # Provenance: the number is tied to the brain that earned it (verified, not None).
    assert sc.knowledge_fingerprint == _brain_fp(project)


def test_release_refuses_a_stale_scorecard(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A held-out run stamped with a DIFFERENT brain than the one shipping must be refused —
    # otherwise the release advertises an accuracy some other version of the files earned.
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))
    dst = tmp_path / "proj"
    shutil.copytree(dogfood_dir, dst)
    project = Project.load(dst)
    paths = SqbylPaths(dst).ensure()
    save_run(
        paths,
        ScoredRun(
            run_id="stale",
            split="test",
            models={"agent": _MODEL},
            knowledge_fingerprint="sha256:not-the-shipped-brain",
            results=[_q("t1", Verdict.correct)],
        ),
    )
    with pytest.raises(ReleaseError, match="different version"):
        build_release(project, "v1")


def test_release_allows_but_flags_an_unverifiable_scorecard(
    tmp_path: Path,
    dogfood_dir: Path,
    duckdb_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A run predating fingerprinting (knowledge_fingerprint None) can't be tied to the files,
    # so it's allowed through but the CLI flags the number as unverified (not silently trusted).
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))
    dst = tmp_path / "proj"
    shutil.copytree(dogfood_dir, dst)
    paths = SqbylPaths(dst).ensure()
    save_run(
        paths,
        ScoredRun(
            run_id="legacy",
            split="test",
            models={"agent": _MODEL},
            results=[_q("t1", Verdict.correct), _q("t2", Verdict.correct)],
        ),
    )
    assert main(["release", "create", "--tag", "v1", str(dst)]) == 0
    assert "provenance unverified" in capsys.readouterr().out


def test_release_validates_against_the_generated_schema(project: Project) -> None:
    # invariant 2: the pydantic model IS the schema authority, and the generated JSON Schema
    # is the public interface. A built release must round-trip through the model *and* carry
    # every field the generated schema names as required.
    release = build_release(project, "v2")
    blob = release.model_dump_json()
    assert ReleaseArtifact.model_validate_json(blob) == release

    schema = release_json_schema()
    payload = json.loads(blob)
    for key in schema["required"]:
        assert key in payload, f"generated schema requires {key!r}, missing from the artifact"
    assert payload["schema_version"] == 1


def test_release_carries_the_schema_fingerprint_and_all_judges(project: Project) -> None:
    from sqbyl.projectfiles import load_semantics

    release = build_release(project, "v1")
    # The fingerprint the runtime will recompute at load() to warn on a renamed table.
    assert release.schema_fingerprint == fingerprint_semantics(load_semantics(project))
    assert release.schema_fingerprint.startswith("sha256:")
    # Every judge prompt is embedded, so a shipped agent's judging is self-describing.
    assert set(release.judges) == set(ALL_JUDGES)
    assert all(release.judges[name].prompt.strip() for name in ALL_JUDGES)


def test_release_requires_a_held_out_run(
    tmp_path: Path, dogfood_dir: Path, duckdb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No persisted test run → refuse: the headline accuracy is always the held-out number.
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))
    dst = tmp_path / "proj"
    shutil.copytree(dogfood_dir, dst)
    SqbylPaths(dst).ensure()
    with pytest.raises(ReleaseError, match="held-out"):
        build_release(Project.load(dst), "v1")


# ── the CLI: writes the file, spends nothing, touches no DB ──────────────────────────────


def test_release_cli_writes_the_conventional_filename(
    project: Project, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Building a release is a pure file op: it must never build an LLM client or connect.
    monkeypatch.setattr(
        "sqbyl.llm.build_llm_client",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("release must not build a client")),
    )
    monkeypatch.setattr(
        "sqbyl.project.Project.connect",
        lambda self: (_ for _ in ()).throw(AssertionError("release must not connect to the DB")),
    )
    code = main(["release", "create", "--tag", "v3", str(project.root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "held-out test" in out and "75%" in out

    written = project.root / "revenue-analytics.v3.json"
    assert written.exists()
    reloaded = ReleaseArtifact.model_validate_json(written.read_text())
    assert reloaded.tag == "v3"
    assert release_filename(reloaded) == "revenue-analytics.v3.json"
    # Nothing was metered — no usage.db rows appeared for the release.
    assert not (SqbylPaths(project.root).usage_db).exists() or _usage_empty(project.root)


def test_release_cli_needs_a_tag(project: Project, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["release", "create", str(project.root)]) == 2
    assert "--tag" in capsys.readouterr().out


def test_release_cli_reports_a_large_overfitting_gap(
    tmp_path: Path,
    dogfood_dir: Path,
    duckdb_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # dev 100% vs held-out 50% is a 50-point gap — the warning must fire (spec §11).
    monkeypatch.setenv("DATABASE_URL", str(duckdb_path))
    dst = tmp_path / "proj"
    shutil.copytree(dogfood_dir, dst)
    paths = SqbylPaths(dst).ensure()
    save_run(
        paths,
        ScoredRun(
            run_id="dev",
            split="dev",
            models={"agent": _MODEL},
            results=[_q(f"d{i}", Verdict.correct) for i in range(4)],
        ),
    )
    save_run(
        paths,
        ScoredRun(
            run_id="test",
            split="test",
            models={"agent": _MODEL},
            results=[_q("t1", Verdict.correct), _q("t2", Verdict.incorrect)],
        ),
    )
    assert main(["release", "create", "--tag", "v1", str(dst)]) == 0
    assert "overfitting" in capsys.readouterr().out.lower()


def _usage_empty(root: Path) -> bool:
    from sqbyl_runtime.state.usage import UsageStore

    with UsageStore(SqbylPaths(root).usage_db) as store:
        return list(store.all()) == []
