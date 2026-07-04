"""Context selection for large schemas (spec §5 step 1, §5.1, plan 9.1).

The context compiler (§5 step 2) defaults to "include everything", which is the
right posture for the ≤5–30-table projects sqbyl steers you toward. Past that, the
prompt gets expensive and noisy, so this module narrows the tables (and the examples
that reference them) to the subset a question actually needs — the "resolve & select"
step that runs *before* compilation.

Four strategies, escalating in cost, all on the single Anthropic key (**no embeddings /
vector store** — spec §13):

* ``include_all`` — every table (the small-project default; deterministic, $0).
* ``lexical`` — rank tables by token overlap between the question and each table's
  name/synonyms/description/columns; keep the top ``max_tables`` (deterministic, $0).
* ``llm`` — Claude shortlists from a compact catalog (names + one-line descriptions).
* ``llm_lexical`` — lexical prefilter to a candidate pool, then Claude picks from *that*
  (a smaller catalog ⇒ a cheaper, more focused call on very wide schemas).

Independently, **value-matching** (spec §5.1) maps high-cardinality literals in the
question ("EMEA") to the canonical declared value ("region = 'emea'"), using only the
``sample_values`` already on each column — which are ``None`` for PII columns, so this
never surfaces a suppressed value.

This lives in ``sqbyl-runtime`` because selection runs at inference time in a shipped
release, not just in dev. It is a **separately-evaluable component** (spec §5.1): the
dev toolkit scores it on its own in ``sqbyl.eval.selection``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from pydantic import BaseModel, Field

from sqbyl_runtime.llm.base import LLMClient, LLMRequest, LLMResponse, Message, Usage
from sqbyl_runtime.models import Column, SelectionConfig, TableSemantics

# Invoked with the shortlisting call's request/response so the caller (the pipeline) can
# emit an OTel GenAI span for it (invariant 7). ``selection.py`` stays tracing-agnostic.
LLMCallHook = Callable[[LLMRequest, LLMResponse], None]

# When a strategy narrows but ``max_tables`` isn't set, this many tables is a sane cap:
# enough headroom for a multi-join question, still far under the include-all ceiling.
_DEFAULT_SELECT_CAP = 10
# ``llm_lexical`` sends the LLM only the lexical top-N; wider than the final cap so the
# model still has real choice, but far narrower than a 200-table catalog.
_LEXICAL_PREFILTER_MULTIPLE = 3
# Value-matching noise guard: never emit more than this many hints for one question.
_MAX_VALUE_MATCHES = 12
# Match tokens of at least this length, so "a"/"of" don't spuriously hit a table.
_MIN_TOKEN_LEN = 3

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class ValueMatch(BaseModel):
    """A question literal mapped to a declared canonical value (spec §5.1).

    ``term`` is the token as it appeared in the question; ``value`` is the column's
    declared sample value it matched (case-insensitively). Rendered as a grounding
    hint so the agent filters on the canonical form rather than guessing.
    """

    term: str
    table: str
    column: str
    value: str


class Selection(BaseModel):
    """The narrowed context for one question: which tables survived, plus value hints.

    ``usage`` carries any tokens an LLM strategy spent so the pipeline meters selection
    against the same budget as generation (invariant 5). ``notes`` explain narrowing
    decisions for the trace/console.
    """

    strategy: str = "include_all"
    tables: list[str] = Field(default_factory=list)
    value_matches: list[ValueMatch] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    # ``True`` when a narrowing strategy degraded to include-all (the shortlist matched no
    # tables, or the LLM returned nothing usable). Surfaced so a silently-broken selector —
    # always reverting to the expensive path it was meant to replace — is observable, not
    # masked (ml-systems / responsible-ai).
    fell_back: bool = False


class _TableShortlist(BaseModel):
    """Strict-JSON shape Claude returns from the shortlisting call."""

    tables: list[str] = Field(default_factory=list)


def select_context(
    question: str,
    *,
    semantics: Sequence[TableSemantics],
    config: SelectionConfig | None = None,
    llm: LLMClient | None = None,
    model: str | None = None,
    on_llm_call: LLMCallHook | None = None,
) -> Selection:
    """Pick the relevant subset of ``semantics`` for ``question`` per ``config``.

    Returns a :class:`Selection` naming the surviving tables (a subset of the input,
    in the input's order) plus any value-matches. Falls back to include-all — never
    an empty schema — if a strategy would drop every table, since the agent can't
    answer with nothing (a note records the fallback). LLM strategies require ``llm``
    and ``model``; without them they degrade to ``lexical`` rather than raise, so a
    caller that forgot to wire the client still gets a usable narrowing.
    """
    config = config or SelectionConfig()
    all_tables = [t.table for t in semantics]
    value_matches = _match_values(question, semantics) if config.value_matching else []
    notes: list[str] = []

    strategy = config.strategy
    if strategy in ("llm", "llm_lexical") and (llm is None or model is None):
        notes.append(f"{strategy!r} selection needs an LLM client; falling back to lexical")
        strategy = "lexical"

    if strategy == "include_all":
        return Selection(
            strategy="include_all", tables=all_tables, value_matches=value_matches, notes=notes
        )

    cap = config.max_tables or _DEFAULT_SELECT_CAP
    ranked = _lexical_rank(question, semantics)

    if strategy == "lexical":
        chosen = [name for name, score in ranked[:cap] if score > 0]
    else:  # llm or llm_lexical
        assert llm is not None and model is not None  # narrowed above
        catalog = semantics
        if strategy == "llm_lexical":
            pool = {name for name, _ in ranked[: cap * _LEXICAL_PREFILTER_MULTIPLE]}
            catalog = [t for t in semantics if t.table in pool] or list(semantics)
        chosen, usage, llm_notes = _llm_shortlist(
            question,
            catalog=catalog,
            valid=set(all_tables),
            llm=llm,
            model=model,
            on_llm_call=on_llm_call,
        )
        notes.extend(llm_notes)
        if chosen:
            return Selection(
                strategy=strategy,
                tables=_in_input_order(chosen, all_tables)[:cap],
                value_matches=value_matches,
                notes=notes,
                usage=usage,
            )
        # An LLM that returned nothing usable is a fallback, but we still spent tokens.
        return _fallback_all(all_tables, value_matches, notes, usage=usage)

    if not chosen:
        return _fallback_all(all_tables, value_matches, notes)
    return Selection(
        strategy=strategy,
        tables=_in_input_order(chosen, all_tables),
        value_matches=value_matches,
        notes=notes,
    )


def _fallback_all(
    all_tables: list[str],
    value_matches: list[ValueMatch],
    notes: list[str],
    *,
    usage: Usage | None = None,
) -> Selection:
    notes.append("selection matched no tables; including all (schema can't be empty)")
    return Selection(
        strategy="include_all",
        tables=all_tables,
        value_matches=value_matches,
        notes=notes,
        usage=usage or Usage(),
        fell_back=True,
    )


# --- lexical ranking (deterministic, $0) ----------------------------------------


def _lexical_rank(question: str, semantics: Sequence[TableSemantics]) -> list[tuple[str, int]]:
    """Rank tables by how many distinct question tokens appear in their vocabulary.

    Deterministic and stable: ties keep the input order (Python's sort is stable and
    we enumerate in order), so the same question always narrows the same way.
    """
    q_tokens = _tokens(question)
    scored: list[tuple[str, int]] = []
    for table in semantics:
        vocab = _table_vocab(table)
        score = sum(1 for tok in q_tokens if tok in vocab)
        scored.append((table.table, score))
    # Stable sort by descending score; equal scores retain input order.
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored


def _table_vocab(table: TableSemantics) -> set[str]:
    """Every token a question could match a table on: its name, synonyms, description,
    and each column's name/synonyms/description."""
    vocab: set[str] = set()
    vocab.update(_tokens(table.table))
    vocab.update(_tokens(table.description or ""))
    for syn in table.synonyms:
        vocab.update(_tokens(syn))
    for col in table.columns:
        vocab.update(_tokens(col.name))
        vocab.update(_tokens(col.description or ""))
        for syn in col.synonyms:
            vocab.update(_tokens(syn))
    return vocab


def _tokens(text: str) -> set[str]:
    """Lowercase alphanumeric tokens of a usable length (drops stopword-ish shorties)."""
    return {t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= _MIN_TOKEN_LEN}


# --- LLM shortlisting ------------------------------------------------------------


def _llm_shortlist(
    question: str,
    *,
    catalog: Sequence[TableSemantics],
    valid: set[str],
    llm: LLMClient,
    model: str,
    on_llm_call: LLMCallHook | None = None,
) -> tuple[list[str], Usage, list[str]]:
    """Ask Claude to name the tables needed for ``question`` from a compact catalog.

    Returns ``(tables, usage, notes)``. Any name the model invents that isn't in
    ``valid`` is dropped (a note records it) — the model shortlists, it doesn't get to
    introduce tables that don't exist. The request/response are handed to ``on_llm_call``
    (if given) so the caller can trace this as its own GenAI span.
    """
    request = LLMRequest(
        model=model,
        messages=[Message(role="user", content=_shortlist_prompt(question, catalog))],
        system=_SHORTLIST_SYSTEM,
        response_schema=_TableShortlist.model_json_schema(),
        max_tokens=512,
        temperature=0.0,
    )
    response = llm.complete(request)
    if on_llm_call is not None:
        on_llm_call(request, response)
    shortlist = response.parse(_TableShortlist)
    notes: list[str] = []
    kept: list[str] = []
    for name in shortlist.tables:
        if name in valid:
            kept.append(name)
        else:
            notes.append(f"selector named unknown table {name!r}; dropped")
    return kept, response.usage, notes


_SHORTLIST_SYSTEM = (
    "You select which database tables are needed to answer a question. Return only the "
    "tables that are actually required — omit the rest. Never invent a table that is not "
    "in the catalog. When unsure between a few, include them; when a table is clearly "
    "irrelevant, leave it out."
)


def _shortlist_prompt(question: str, catalog: Sequence[TableSemantics]) -> str:
    lines = ["Catalog of available tables:"]
    for table in catalog:
        desc = f" — {table.description}" if table.description else ""
        syn = f" (aka {', '.join(table.synonyms)})" if table.synonyms else ""
        lines.append(f"- {table.table}{desc}{syn}")
    lines.append("")
    lines.append(f"Question: {question.strip()}")
    lines.append("")
    lines.append("Return the tables needed to answer it as a JSON list of exact table names.")
    return "\n".join(lines)


# --- value-matching (spec §5.1) --------------------------------------------------


def _match_values(question: str, semantics: Sequence[TableSemantics]) -> list[ValueMatch]:
    """Map literals in the question to declared canonical sample values.

    Only ``sample_values`` are consulted, and those are ``None`` for PII columns
    (suppressed by the profiler, spec §13), so a value-match can never surface a
    suppressed value. Matching is case-insensitive on whole tokens; the emitted
    ``value`` is the column's declared form, so the agent filters on the canonical
    spelling ("EMEA" → ``region = 'emea'``).
    """
    q_tokens = _tokens(question)
    matches: list[ValueMatch] = []
    seen: set[tuple[str, str, str]] = set()
    for table in semantics:
        for col in table.columns:
            for sample in _string_samples(col):
                if sample.lower() in q_tokens:
                    key = (table.table, col.name, sample)
                    if key in seen:
                        continue
                    seen.add(key)
                    matches.append(
                        ValueMatch(
                            term=sample.lower(),
                            table=table.table,
                            column=col.name,
                            value=sample,
                        )
                    )
                    if len(matches) >= _MAX_VALUE_MATCHES:
                        return matches
    return matches


def _string_samples(col: Column) -> list[str]:
    """The column's declared string sample values, or ``[]`` if it's opted out of profiling.

    Enforces the PII opt-out on the *read* path, independently of how ``sample_values`` got
    populated: a column marked ``profile: false`` (the human opted it out, spec §13) never
    yields a value hint even if a stale or hand-authored ``sample_values`` block is present.
    The profiler suppresses PII by setting ``sample_values`` to ``None``; this is the belt to
    that suspenders, so a value-match can never surface a value from an opted-out column.
    """
    if col.profile is False or not col.sample_values:
        return []
    return [v for v in col.sample_values if isinstance(v, str)]


# --- helpers ---------------------------------------------------------------------


def _in_input_order(chosen: Sequence[str], all_tables: Sequence[str]) -> list[str]:
    """Return ``chosen`` reordered to match the schema's declared order (stable output)."""
    picked = set(chosen)
    return [name for name in all_tables if name in picked]
