"""Shared test fixtures: repo paths, the seeded DuckDB, and the dogfood project dir."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOGFOOD_DIR = REPO_ROOT / "examples" / "revenue-analytics"
DUCKDB_PATH = REPO_ROOT / "fixtures" / "orders.duckdb"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def dogfood_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A clean copy of the dogfood project, excluding ephemeral local state (``.sqbyl/``).

    Tests copytree this dir into per-test scratch dirs and assert on the usage/trace state
    they create. Handing back the repo dir directly would drag in any ``.sqbyl/`` (usage.db,
    traces, runs) left by a local ``sqbyl`` run, poisoning those assertions. Sanitize once
    per session so a developer running the dogfood project never breaks the suite.
    """
    clean = tmp_path_factory.mktemp("dogfood") / "revenue-analytics"
    shutil.copytree(DOGFOOD_DIR, clean, ignore=shutil.ignore_patterns(".sqbyl"))
    return clean


@pytest.fixture(scope="session")
def duckdb_path() -> Path:
    """Path to the checked-in seeded DuckDB; rebuild deterministically if absent."""
    if not DUCKDB_PATH.exists():
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "build_orders_duckdb", REPO_ROOT / "fixtures" / "build_orders_duckdb.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.build(DUCKDB_PATH)
    return DUCKDB_PATH
