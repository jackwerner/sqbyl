"""Production feedback — the 👍/👎 a `sqbyl serve`/`run` user leaves on an answer.

Closing the §7 loop: an answer a user marks good is an eval/synth *candidate* (a
question paired with SQL that a human blessed); one marked bad is a failure to learn
from. This is the dev-side landing shape for that signal, appended to
`.sqbyl/feedback.jsonl` and later read by synth/eval — so it lives in the dev `sqbyl`
package, not the shippable runtime.

**No row data (spec §13).** A feedback record keeps the *question* and the *SQL* (both
already the user's own authored/generated text) and a rating — never the result rows,
which could carry PII. The `trace_id` links back to the local trace for anyone who
needs the full context.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import Field

from sqbyl_runtime.models import SqbylModel

Rating = Literal["up", "down"]


class FeedbackRecord(SqbylModel):
    """One 👍/👎 on a served answer — an eval/synth candidate, not a score.

    The log is **append-only**, so a mis-clicked rating is corrected by rating again: a
    consumer (synth/eval) must treat the records as **last-write-wins per ``trace_id``**,
    taking the most recent rating for a given answer rather than counting every append. The
    UI leaves both 👍/👎 live after a click so a fat-fingered rating can be reversed without
    it silently seeding the improvement loop.
    """

    trace_id: str
    question: str
    sql: str
    rating: Rating
    ok: bool = Field(description="Whether the answer executed successfully (agent-reported).")
    note: str | None = None
    source: str = Field(default="serve", description="'serve' (dev project) or 'run' (release).")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
