"""Layer-2 LLM judges + the arbiter (spec §7 Layer 2, plan 5.1).

Layer 1 (:mod:`sqbyl.eval.scorers`) parks two kinds of row at ``manual_review``: a
result-set *mismatch* (different rows — but maybe the same meaning) and a question with
*no executable gold*. Neither is proof of incorrectness, so Layer 1 refuses to score them.
Layer 2 resolves them with a small panel of Claude judges — each reading an **editable
``judges/<name>.md`` prompt** — and an **arbiter** that folds the panel into a final
verdict *only when it is unanimous and confident*, otherwise leaving the row at
``manual_review`` for a human rather than silently scoring it (spec §7).

Two invariants shape this module:

* **Passing rows never call a judge.** :func:`adjudicate` short-circuits on any verdict
  that is not ``manual_review``, so a ``correct`` row costs zero tokens (spec §7).
* **This is eval machinery, not the dev loop.** Judges run inside the eval harness, which
  is the one sanctioned reader of the held-out set — so, unlike the Coach, this module is
  *not* under the ``forbidden -> heldout`` import contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import ValidationError

from sqbyl.models import (
    ALL_JUDGES,
    GOLD_MISMATCH_JUDGES,
    NO_GOLD_JUDGES,
    JudgeVerdict,
    Verdict,
)
from sqbyl.project import Project
from sqbyl_runtime.llm.base import LLMClient, LLMRequest, Message, Usage
from sqbyl_runtime.models import Dialect
from sqbyl_runtime.state.traces import TraceWriter, llm_call_span, new_trace_id

# The least-sure judge must clear this bar for the arbiter to score a row automatically;
# below it the row stays ``manual_review`` (spec §7: flag low-confidence, don't silently
# score). A conservative default — a split or unsure panel routes to a human.
DEFAULT_MIN_CONFIDENCE = 0.7

# Bundled default prompts, used when a project ships no ``judges/<name>.md`` override. They
# are deliberately terse and editable — a curator tunes them on disk and versions them in
# git, the opposite of a hosted black-box judge (spec §7). Each judge returns the strict
# JSON of :class:`JudgeVerdict`; the seam forces that shape, so the prompt only has to
# explain *what to judge*, not *how to format*.
_DEFAULT_PROMPTS: dict[str, str] = {
    "semantic_equivalence": (
        "You judge whether two SQL queries are LOGICALLY EQUIVALENT for answering a "
        "business question, even if their result rows differ superficially (extra columns, "
        "different column order or aliases, rounding, or ordering). Pass only if the "
        "generated query would answer the question the same way the gold query does. A "
        "genuinely different computation (wrong aggregation, missing filter, different "
        "grain) is NOT equivalent. Set confidence low when the schema or intent is "
        "ambiguous."
    ),
    "logical_accuracy": (
        "You judge whether the generated SQL correctly implements the intent of the "
        "question given the schema — the right tables, joins, filters, grouping, and "
        "aggregation. Ignore stylistic differences from the gold query; judge correctness "
        "of meaning. Set confidence low when the question is under-specified."
    ),
    "completeness": (
        "You judge whether the generated SQL FULLY answers the question — no missing "
        "filter, group-by, or column the question asks for, and nothing extra that changes "
        "the answer. A partial answer fails. Set confidence low when it is unclear how much "
        "the question demands."
    ),
    "answer_quality": (
        "You judge whether a natural-language answer is grounded in the returned rows and "
        "correctly summarizes them for the question. It must not assert anything the rows "
        "do not support. Use any provided grading note as guidance."
    ),
}


def load_judge_prompt(project: Project, name: str) -> str:
    """The prompt for judge ``name`` — the project's ``judges/<name>.md`` if it exists,
    else the bundled default. Curators edit the file on disk to tune a judge (spec §7)."""
    path = project.root / "judges" / f"{name}.md"
    if path.exists():
        text = path.read_text().strip()
        if text:
            return text
    try:
        return _DEFAULT_PROMPTS[name]
    except KeyError:
        raise ValueError(
            f"unknown judge {name!r}; expected one of {sorted(_DEFAULT_PROMPTS)}"
        ) from None


def load_judge_prompts(project: Project) -> dict[str, str]:
    """Every judge's prompt, project override or bundled default (spec §7).

    Loaded once by the eval caller and passed into :func:`adjudicate`, so the arbiter stays
    project-free and testable with a plain dict — the same seam as the runner's pre-loaded
    ``asset_sql``.
    """
    return {name: load_judge_prompt(project, name) for name in ALL_JUDGES}


@dataclass(frozen=True)
class ArbiterOutcome:
    """The advisory result of triaging one row.

    ``suggestion`` is the arbiter's *hint* to speed a human's review — ``correct`` (likely
    equivalent), ``incorrect`` (likely wrong), or ``manual_review`` (genuinely ambiguous —
    the panel couldn't agree confidently). It is ``None`` when the panel didn't run (the row
    was already resolved by Layer 1). It **never** feeds the headline accuracy; only a human
    can move a row out of the review pile (spec §7, Phase 5.2). ``usage`` accumulates every
    judge call so the row's cost is metered like any paid path.
    """

    suggestion: Verdict | None
    judge_verdicts: list[JudgeVerdict] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


def _render_case(
    *, question: str, generated_sql: str, gold_sql: str | None, dialect: Dialect
) -> str:
    gold_block = gold_sql if gold_sql is not None else "(none — no gold query for this question)"
    return (
        f"SQL dialect: {dialect.value}\n\n"
        f"QUESTION:\n{question}\n\n"
        f"GENERATED SQL (the answer under evaluation):\n{generated_sql}\n\n"
        f"GOLD SQL (the reference answer):\n{gold_block}\n\n"
        "Return your verdict: does the generated SQL earn a PASS on the dimension you "
        "judge? Give a confidence in [0, 1] and a one-sentence rationale."
    )


def run_judge(
    llm: LLMClient,
    name: str,
    prompt: str,
    *,
    question: str,
    generated_sql: str,
    gold_sql: str | None,
    dialect: Dialect,
    model: str,
    trace_writer: TraceWriter | None = None,
    trace_id: str | None = None,
    parent_span_id: str | None = None,
) -> tuple[JudgeVerdict | None, Usage]:
    """One judge, one paid structured call → a :class:`JudgeVerdict` (or ``None``).

    The editable ``prompt`` is the system message; the case is the user message. The
    ``judge`` field is stamped from ``name`` after parsing, so a verdict is always
    attributed to the judge that produced it regardless of what the model echoes. Written
    as an OTel-GenAI span when a ``trace_writer`` is given (invariant 7).

    Returns ``(None, usage)`` if the model's payload can't be validated into a
    :class:`JudgeVerdict`: the call still happened, so its span is written and its tokens are
    returned to be metered (invariant 5) — a flaky judge is dropped from the panel, not
    escalated into a crash.
    """
    request = LLMRequest(
        model=model,
        messages=[
            Message(
                role="user",
                content=_render_case(
                    question=question,
                    generated_sql=generated_sql,
                    gold_sql=gold_sql,
                    dialect=dialect,
                ),
            )
        ],
        system=prompt,
        response_schema=JudgeVerdict.model_json_schema(),
        max_tokens=1024,
        temperature=0.0,
        cache_system=True,
    )
    response = llm.complete(request)
    if trace_writer is not None:
        trace_writer.write(
            llm_call_span(
                request,
                response,
                operation="chat",
                name=f"judge {name}",
                trace_id=trace_id or new_trace_id(),
                parent_span_id=parent_span_id,
            )
        )
    try:
        verdict: JudgeVerdict | None = response.parse(JudgeVerdict).model_copy(
            update={"judge": name}
        )
    except (ValidationError, ValueError):
        verdict = None
    return verdict, response.usage


def fold_panel(
    verdicts: list[JudgeVerdict], *, min_confidence: float = DEFAULT_MIN_CONFIDENCE
) -> Verdict:
    """Fold a judge panel into one **advisory suggestion** — the conservative rule (spec §7).

    The panel offers a confident hint **only** when it is *unanimous and confident*: all
    judges pass at or above ``min_confidence`` → suggest ``correct`` (likely equivalent);
    all fail at or above it → suggest ``incorrect`` (likely wrong). A split panel, or any
    judge below the bar, yields ``manual_review`` — "genuinely ambiguous, look closely".

    This is a triage hint to speed a human, not a score: the row stays in the review pile
    either way (the headline accuracy is deterministic). ``min_confidence`` is an unvalidated
    heuristic, not a calibrated threshold — it will be re-derived against human overrides
    once the calibration set exists (Phase 5.2).
    """
    if not verdicts:
        return Verdict.manual_review
    if min(v.confidence for v in verdicts) < min_confidence:
        return Verdict.manual_review
    if all(v.passed for v in verdicts):
        return Verdict.correct
    if not any(v.passed for v in verdicts):
        return Verdict.incorrect
    return Verdict.manual_review


def adjudicate(
    llm: LLMClient,
    *,
    verdict: Verdict,
    question: str,
    generated_sql: str,
    gold_sql: str | None,
    prompts: dict[str, str],
    dialect: Dialect,
    model: str,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    trace_writer: TraceWriter | None = None,
    trace_id: str | None = None,
    parent_span_id: str | None = None,
) -> ArbiterOutcome:
    """Triage one row: run the applicable judges and fold them into an advisory suggestion.

    Short-circuits — costing **zero tokens** — on any Layer-1 verdict that is not
    ``manual_review`` (a ``correct`` or ``error`` row has nothing to triage, spec §7).
    Otherwise it selects the judges that apply — the full mismatch panel when a gold query
    exists, the gold-free subset when it does not — runs each, and returns their verdicts
    plus the arbiter's :func:`fold_panel` suggestion and the summed usage.

    A single judge returning off-schema JSON must not sink a whole eval run: its call is
    skipped (its tokens still counted) and the suggestion is folded from the judges that did
    parse. If none parse, the suggestion is ``manual_review`` — exactly where the row
    already sits, so a flaky judge degrades to "a human looks", never to a lost run.

    ``prompts`` maps judge name → prompt text (see :func:`load_judge_prompts`), keeping the
    arbiter project-free.
    """
    if verdict is not Verdict.manual_review:
        return ArbiterOutcome(suggestion=None)

    names = GOLD_MISMATCH_JUDGES if gold_sql is not None else NO_GOLD_JUDGES
    trace_id = trace_id or new_trace_id()
    verdicts: list[JudgeVerdict] = []
    usage = Usage()
    for name in names:
        judged, judge_usage = run_judge(
            llm,
            name,
            prompts[name],
            question=question,
            generated_sql=generated_sql,
            gold_sql=gold_sql,
            dialect=dialect,
            model=model,
            trace_writer=trace_writer,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
        )
        usage = usage + judge_usage  # count the call even if its payload was malformed
        if judged is not None:  # a dropped (unparseable) judge simply isn't a panel vote
            verdicts.append(judged)

    suggestion = fold_panel(verdicts, min_confidence=min_confidence)
    return ArbiterOutcome(suggestion=suggestion, judge_verdicts=verdicts, usage=usage)
