"""The ``.sqbyl/`` on-disk layout (spec §4, §3 #7).

Local-first state: usage accounting (SQLite), traces (JSONL), run history. Kept
beside the project and gitignored. The runtime owns this because a shipped agent
is "a model with logs" — it writes usage and traces even in production.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SqbylPaths:
    """Resolved paths under a project's ``.sqbyl/`` directory."""

    project_root: Path

    @property
    def root(self) -> Path:
        return self.project_root / ".sqbyl"

    @property
    def usage_db(self) -> Path:
        return self.root / "usage.db"

    @property
    def traces_dir(self) -> Path:
        return self.root / "traces"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def feedback_log(self) -> Path:
        """Append-only 👍/👎 from `sqbyl serve`/`run` — eval/synth candidates (spec §7)."""
        return self.root / "feedback.jsonl"

    def ensure(self) -> SqbylPaths:
        """Create the directory skeleton if missing. Returns self for chaining."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.traces_dir.mkdir(exist_ok=True)
        self.runs_dir.mkdir(exist_ok=True)
        return self
