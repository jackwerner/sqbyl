"""Judge prompts (spec §7 Layer 2).

LLM-judge prompts live in editable ``judges/*.md`` files and are embedded in a
release so a shipped agent's judging behavior is reproducible and inspectable.
The runtime carries the model only so releases stay self-describing; the judges
themselves run in the dev toolkit.
"""

from __future__ import annotations

from sqbyl_runtime.models.base import SqbylModel


class JudgePrompt(SqbylModel):
    """A single named LLM judge (e.g. ``semantic_equivalence``)."""

    name: str
    description: str | None = None
    prompt: str
