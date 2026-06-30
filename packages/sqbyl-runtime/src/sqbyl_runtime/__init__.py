"""sqbyl-runtime — the minimal, shippable sqbyl runtime.

Contains only what a production app needs to embed a released agent: release
``load()``, ``ask()``, the ``LLMClient`` seam, and structured logging. None of
the dev toolkit (eval, synth, Coach, judges, console) lives here or is importable
from here — that one-way dependency arrow is enforced by import-linter in CI.
"""

__version__ = "0.0.0"
