"""The eval harness — *measure* the agent (spec §7, plan Phase 3).

Layer 1 (this phase) is deterministic and cheap: it runs always and is the primary,
objective signal. The expensive LLM judges (Layer 2) and the Coach build on top of it
in later phases. Everything here is dev machinery and lives in the ``sqbyl`` package
(invariant 1); the held-out ``test.yaml`` is reachable only through
:mod:`sqbyl.eval.benchmarks_io` (invariant 3).
"""

from __future__ import annotations
