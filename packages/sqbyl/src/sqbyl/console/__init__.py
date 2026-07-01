"""The local review console — ``sqbyl review`` (spec §6.5, plan 4.2).

A FastAPI app with a small bundled UI, **no cloud, no account**. It is a thin surface over
the project files: everything it writes lands back in ``benchmarks/dev.yaml`` (via the
dev-hard-wired writer), never in a second source of truth and never in the held-out
``test.yaml`` (invariant 3). This phase ships the golden-set review; later queues (judge
verdicts, Coach proposals) reuse the same keyboard-driven interaction model.
"""

from __future__ import annotations

from sqbyl.console.app import create_app

__all__ = ["create_app"]
