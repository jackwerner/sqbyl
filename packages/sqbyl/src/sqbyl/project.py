"""Loading a sqbyl project from disk (spec §4).

A project is a plain directory: ``sqbyl.yaml`` plus ``semantics/``, ``examples/``,
``benchmarks/`` … . ``Project`` is the small handle the dev commands share for
finding the manifest and opening the (read-only) database it points at.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqbyl.models import SqbylManifest
from sqbyl.yamlio import load_yaml
from sqbyl_runtime.db import Database

if TYPE_CHECKING:
    from sqbyl.models.kpis import KpiReport
    from sqbyl.models.runs import ScoredRun
    from sqbyl_runtime.llm.base import LLMClient


@dataclass(frozen=True)
class Project:
    """A loaded sqbyl project rooted at a directory."""

    root: Path
    manifest: SqbylManifest

    @classmethod
    def load(cls, root: str | Path) -> Project:
        root = Path(root)
        manifest_path = root / "sqbyl.yaml"
        if not manifest_path.exists():
            raise FileNotFoundError(f"no sqbyl.yaml found in {root}")
        manifest = SqbylManifest.model_validate(load_yaml(manifest_path.read_text()))
        return cls(root=root, manifest=manifest)

    @property
    def semantics_dir(self) -> Path:
        return self.root / "semantics"

    def connect(self) -> Database:
        """Open the project's database, read-only by default per the manifest."""
        db = self.manifest.database
        return Database.connect(db.url, dialect=db.dialect, read_only=db.read_only)

    def eval(
        self,
        split: str = "dev",
        *,
        llm: LLMClient | None = None,
        replay: str | Path | None = None,
        record: str | Path | None = None,
        as_of: datetime | None = None,
        judge: bool | None = None,
        persist: bool = True,
    ) -> ScoredRun:
        """Run the eval harness over a benchmark ``split`` → :class:`ScoredRun` (spec §10).

        The substrate for ``sqbyl eval``: builds the LLM client (unless one is injected,
        e.g. a mock/replay client in tests), runs every question, **meters each paid call**
        to ``.sqbyl/usage.db`` (invariant 5), and persists the run to ``.sqbyl/runs/``.

        ``judge`` forces Layer-2 judging on/off (default follows ``automation.auto_judge``);
        the optimizer passes ``judge=False`` so its many trial evals stay cheap and
        deterministic (the judge is advisory and never moves the headline it optimizes).
        """
        from sqbyl.eval.report import save_run
        from sqbyl.eval.runner import run_eval
        from sqbyl.llm import build_llm_client
        from sqbyl_runtime.state.layout import SqbylPaths
        from sqbyl_runtime.state.traces import TraceWriter
        from sqbyl_runtime.state.usage import UsageRecord, UsageStore

        client = llm or build_llm_client(self.manifest, replay=replay, record=record)
        paths = SqbylPaths(self.root).ensure()
        run = run_eval(
            self,
            split=split,
            llm=client,
            as_of=as_of,
            judge=judge,
            trace_writer=TraceWriter(paths.traces_dir / "eval.jsonl"),
        )
        with UsageStore(paths.usage_db) as store:
            for result in run.results:
                store.record(
                    UsageRecord.from_usage(
                        result.usage,
                        model=run.models.get("agent"),
                        command="eval",
                        role="agent",
                        cost_usd=result.cost_usd,
                        run_id=run.run_id,
                    )
                )
                # Layer-2 judging is metered as its own role so cost is attributed to the
                # judge model, not folded into the agent's (invariant 5, §7.5).
                if result.judge_usage.total_tokens:
                    store.record(
                        UsageRecord.from_usage(
                            result.judge_usage,
                            model=run.models.get("judge"),
                            command="eval",
                            role="judge",
                            cost_usd=result.judge_cost_usd,
                            run_id=run.run_id,
                        )
                    )
        if persist:
            save_run(paths, run)
        return run

    def kpis(self, *, volume: int | None = None) -> KpiReport:
        """Roll ``.sqbyl/`` (usage + runs + latencies) into a :class:`KpiReport` (spec §7.5).

        A pure reporting view: spends no tokens, opens no DB connection, and emits aggregates
        only (§13). ``volume`` (queries/month) adds a projected run-rate. Dev and held-out
        test are reported separately, never conflated.
        """
        from sqbyl.kpis import build_report

        return build_report(self, volume=volume)
