"""Guided ``sqbyl init`` — the push, from a bare connection to a review queue (spec §5.5).

This is the command that ties the whole engine together, `sam deploy --guided`-style:

  1. **Free pass** (``$0``): connect → introspect → profile → heuristic joins. Deterministic,
     read-only SQL; nothing is spent and you see what you've got before any confirmation.
  2. **Costed plan**: an itemized :class:`~sqbyl_runtime.cost.CostEstimate` for the paid
     enrichment (annotate + synth + baseline eval), so the number you approve is a cap.
  3. **Stepped enrichment** (after you confirm): the approved work runs through the Phase 6
     :class:`~sqbyl.orchestrator.Orchestrator` — annotation fanned out in parallel under the
     budget, then execution-grounded synth, a baseline eval, and (if ``auto_coach``) the Coach
     — metering live, ending in the Phase 6 attention queue.

Re-running ``init`` re-orchestrates **only what changed**: a per-file digest (Phase 0.5) is
recorded when a table is annotated, so an unchanged re-run does no paid work at all.

The module is the substrate; ``cli._init`` is the thin shell (Python API is the substrate,
the CLI is a wrapper — spec §10). Every paid stage takes an injected ``llm`` so the whole
journey runs under record-replay with zero tokens (invariant 4).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from sqbyl.attention import (
    decisions_from_coach_report,
    decisions_from_outcomes,
    decisions_from_review_pile,
    route,
)
from sqbyl.estimates import annotate_estimate, eval_estimate, synth_estimate
from sqbyl.introspect import introspect
from sqbyl.profile import profile_table
from sqbyl.semantics_io import (
    dump_yaml_path,
    load_for_profiling,
    merge_annotation,
    merge_profiles,
    table_filename,
    write_draft,
)
from sqbyl.state.hashing import file_digest
from sqbyl_runtime.cost import CostEstimate, SpendMeter, price_usage
from sqbyl_runtime.fingerprint import fingerprint_semantics
from sqbyl_runtime.models import SqbylModel, TableSemantics
from sqbyl_runtime.state.layout import SqbylPaths

if TYPE_CHECKING:
    from sqbyl.models.attention import AttentionQueue
    from sqbyl.models.coach import CoachReport
    from sqbyl.models.runs import ScoredRun
    from sqbyl.orchestrator import Orchestrator, OrchestratorResult
    from sqbyl.project import Project
    from sqbyl_runtime.llm.base import LLMClient

# Steps a user can drop with `--select` / the `[s]elect` menu.
STEPS = ("annotate", "synth", "eval")

# Gate signature: (meter, next_step_estimate, human-readable label) -> proceed?
# The CLI supplies the guided-pause / --auto hard-stop policy; init stays UI-agnostic.
AuthorizeFn = Callable[[SpendMeter, float, str], bool]


# ── the free pass (spec §5.5 Phase 1 — $0) ──────────────────────────────────────────────


@dataclass
class FreePass:
    """What the deterministic, read-only pass found — shown before anything is spent."""

    tables: list[TableSemantics]
    n_columns: int
    joins: int
    ambiguous_joins: int
    schema_fingerprint: str

    @property
    def n_tables(self) -> int:
        return len(self.tables)


def _schema_fingerprint(raw_tables: list[TableSemantics]) -> str:
    """A stable hash of the live DB schema (tables + column names/types), independent of the
    semantics YAML. The content hash covers only project *files*, so a schema/data change that
    doesn't touch the YAML would otherwise leave the baseline-skip gate blind to it — this is
    what makes ``init`` re-eval when the database itself moved (ml-systems: measurement validity).

    The same hash a release is stamped with (:mod:`sqbyl_runtime.fingerprint`), so the
    ``init`` baseline gate and a shipped release speak of the schema identically.
    """
    return fingerprint_semantics(raw_tables)


def run_free_pass(project: Project, *, force: bool = False) -> FreePass:
    """Connect → introspect → profile every table, writing drafts + ``profile:`` blocks.

    All read-only SQL, no LLM (``$0``). Existing semantics files are preserved (a re-run
    doesn't clobber hand edits) unless ``force``; profiling refreshes the deterministic
    stats in place. Returns the profiled semantics, the join-candidate counts, and a
    fingerprint of the live schema (for the incremental re-eval gate).
    """
    project.semantics_dir.mkdir(parents=True, exist_ok=True)
    with project.connect() as db:
        raw_tables = introspect(db)
        for table in raw_tables:
            path = project.semantics_dir / table_filename(table.table)
            if not path.exists() or force:
                write_draft(table, path)

        profiled: list[TableSemantics] = []
        for path in sorted(project.semantics_dir.glob("*.yaml")):
            loaded = load_for_profiling(path)
            if loaded.table_skipped:
                profiled.append(loaded.table)
                continue
            result = profile_table(db, loaded.table, options=loaded.options)
            dump_yaml_path(merge_profiles(loaded, result), path)
            profiled.append(result)

    joins = [j for t in profiled for j in t.joins]
    # FK-derived joins carry no confidence (certain); heuristic candidates carry a <1 score.
    ambiguous = sum(1 for j in joins if j.confidence is not None and j.confidence < 1.0)
    n_columns = sum(len(t.columns) for t in profiled)
    return FreePass(
        tables=profiled,
        n_columns=n_columns,
        joins=len(joins),
        ambiguous_joins=ambiguous,
        schema_fingerprint=_schema_fingerprint(raw_tables),
    )


# ── the costed plan (spec §5.5 Phase 1 — the estimate you approve) ───────────────────────


@dataclass
class InitPlan:
    """The paid work ``init`` proposes, itemized — a cap the user confirms (or trims)."""

    model: str
    annotate_tables: list[str]
    synth_n: int
    do_synth: bool
    do_eval: bool
    eval_questions: int
    estimate: CostEstimate

    @property
    def has_paid_work(self) -> bool:
        return bool(self.estimate.items)


class EnrichmentState(SqbylModel):
    """``.sqbyl/enrichment.json`` — what ``init`` has already orchestrated, so a re-run can
    skip unchanged work (spec §5.5). A pydantic model like every other persisted artifact
    (invariant 2): the shape, defaults, and (de)serialization all flow from here."""

    annotated: dict[str, str] = {}  # semantics filename -> file digest at last annotation
    baseline_hash: str | None = None  # project content hash when the baseline eval last ran
    schema_fingerprint: str | None = None  # live-schema fingerprint at that baseline
    baseline_as_of: str | None = None  # the --as-of the baseline was computed at (ISO or None)


def _enrichment_path(project: Project) -> Path:
    return SqbylPaths(project.root).root / "enrichment.json"


def _load_enrichment(project: Project) -> EnrichmentState:
    """The incremental-orchestration state (a default-empty model when absent)."""
    path = _enrichment_path(project)
    return (
        EnrichmentState.model_validate_json(path.read_text())
        if path.exists()
        else EnrichmentState()
    )


def _save_enrichment(project: Project, state: EnrichmentState) -> None:
    path = _enrichment_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.model_dump_json(indent=2) + "\n")


def _record_annotated(project: Project, table_file: Path) -> None:
    """Stamp a table's post-annotation digest so an unchanged re-run skips it next time."""
    state = _load_enrichment(project)
    state.annotated = {**state.annotated, table_file.name: file_digest(table_file)}
    _save_enrichment(project, state)


def _record_baseline(project: Project, *, schema_fingerprint: str, as_of: datetime | None) -> None:
    """Stamp what the baseline eval ran against — content hash, schema fingerprint, and
    ``as_of`` — so an unchanged re-run skips re-evaluating but any of them moving forces it."""
    from sqbyl.state.hashing import content_hash

    state = _load_enrichment(project)
    state.baseline_hash = content_hash(project.root)
    state.schema_fingerprint = schema_fingerprint
    state.baseline_as_of = as_of.isoformat() if as_of is not None else None
    _save_enrichment(project, state)


def _baseline_is_current(
    project: Project, *, schema_fingerprint: str, as_of: datetime | None
) -> bool:
    """True when a baseline eval already ran at the project's current content hash **and**
    schema fingerprint **and** ``as_of`` — so a changed DB schema or a different ``--as-of``
    (time-relative gold) both force a re-eval rather than serving a stale number."""
    from sqbyl.eval.report import latest_run
    from sqbyl.state.hashing import content_hash

    state = _load_enrichment(project)
    if state.baseline_hash != content_hash(project.root):
        return False
    if state.schema_fingerprint != schema_fingerprint:
        return False
    if state.baseline_as_of != (as_of.isoformat() if as_of is not None else None):
        return False
    return latest_run(SqbylPaths(project.root), split="dev") is not None


def tables_needing_annotation(project: Project) -> list[Path]:
    """Semantics files that are new, un-annotated, or changed since they were annotated.

    A table is skipped only when it still carries a description *and* its file digest matches
    the one stamped at its last annotation — so `init` re-annotates a table when, and only
    when, its content moved (spec §5.5 "re-orchestrates only what changed").
    """
    from sqbyl.yamlio import load_yaml

    digests = _load_enrichment(project).annotated
    needing: list[Path] = []
    for path in sorted(project.semantics_dir.glob("*.yaml")):
        raw = load_yaml(path.read_text())
        described = bool(str(raw.get("description") or "").strip())
        unchanged = digests.get(path.name) == file_digest(path)
        if not (described and unchanged):
            needing.append(path)
    return needing


def _dev_question_count(project: Project) -> int:
    from sqbyl.eval.benchmarks_io import dev_set_size

    return dev_set_size(project)


def build_plan(
    project: Project,
    free: FreePass,
    *,
    model: str,
    steps: tuple[str, ...] = STEPS,
    synth_n: int = 20,
    as_of: datetime | None = None,
) -> InitPlan:
    """Compose the costed plan from what the free pass found and what's already done.

    Only un-done work is priced: tables already annotated (and unchanged) drop out of the
    annotate line, synth is skipped when a dev set already exists, and the baseline eval is
    skipped when one already ran at the current content hash / schema / ``as_of``. So a
    fully-enriched, unchanged project plans ``$0``.
    """
    repairs = project.manifest.defaults.self_repair_attempts
    annotate_paths = tables_needing_annotation(project) if "annotate" in steps else []
    annotate_names = [p.name for p in annotate_paths]

    existing_dev = _dev_question_count(project)
    do_synth = "synth" in steps and existing_dev == 0  # only cold-start synthesizes
    # Skip the baseline eval when one already ran against this exact project + schema + clock
    # (nothing changed); synth will change the content hash, so a cold-start run always evaluates.
    baseline_current = _baseline_is_current(
        project, schema_fingerprint=free.schema_fingerprint, as_of=as_of
    )
    do_eval = "eval" in steps and not (not do_synth and baseline_current)
    # After synth, the dev set is ~synth_n survivors; otherwise it's what's already there.
    eval_questions = (synth_n if do_synth else existing_dev) if do_eval else 0

    items = []
    if annotate_paths:
        items += annotate_estimate(model, tables=len(annotate_paths)).items
    if do_synth:
        items += synth_estimate(project.manifest.model.for_role("synth"), n=synth_n).items
    if do_eval and eval_questions:
        judge = (
            project.manifest.model.for_role("judge")
            if project.manifest.automation.auto_judge
            else None
        )
        items += eval_estimate(
            model,
            questions=eval_questions,
            judge_model=judge,
            self_repair_attempts=repairs,
        ).items

    return InitPlan(
        model=model,
        annotate_tables=annotate_names,
        synth_n=synth_n,
        do_synth=do_synth,
        do_eval=do_eval and eval_questions > 0,
        eval_questions=eval_questions,
        estimate=CostEstimate(items=items),
    )


# ── the stepped enrichment (spec §5.5 Phase 2 — after confirmation) ──────────────────────


@dataclass
class EnrichmentResult:
    """What the paid enrichment produced — folded into the arrival summary + queue."""

    annotated: int = 0
    annotate_failures: list[tuple[str, str]] = field(default_factory=list)
    survivors: int = 0
    run: ScoredRun | None = None
    coach_report: CoachReport | None = None
    queue: AttentionQueue | None = None
    spent_usd: float = 0.0
    stopped: bool = False  # a budget stop left work undone


def _annotate_wave(
    project: Project,
    plan: InitPlan,
    *,
    llm: LLMClient,
    meter: SpendMeter,
    orchestrator: Orchestrator,
) -> tuple[int, list[tuple[str, str]], OrchestratorResult[Path]]:
    """Fan the per-table annotation out through the Phase 6 orchestrator, under the budget.

    The orchestrator's own pre-dispatch budget gate bounds the concurrent wave (the meter's
    cap is check-then-act and not concurrency-safe — see :class:`SpendMeter`); each finished
    unit is then metered durably to ``.sqbyl/usage.db`` via the shared meter. A table that
    fails to annotate degrades to a card (its siblings still complete) — spec §5.5.
    """
    from sqbyl.annotate import annotate_table
    from sqbyl.orchestrator import WorkProduct, WorkUnit
    from sqbyl.yamlio import load_yaml
    from sqbyl_runtime.state.traces import TraceWriter, new_trace_id

    model = plan.model
    paths = [project.semantics_dir / name for name in plan.annotate_tables]
    # Same OTel-GenAI trace the standalone `sqbyl annotate` writes (invariant 7): the paid
    # work is traced identically whether it runs here or on its own.
    trace_writer = TraceWriter(SqbylPaths(project.root).ensure().traces_dir / "annotate.jsonl")

    def make(path: Path, prime: bool) -> WorkUnit[Path]:
        def run() -> WorkProduct[Path]:
            raw = load_yaml(path.read_text())
            table = TableSemantics.model_validate(raw)
            annotation, response = annotate_table(
                llm, table, model=model, trace_writer=trace_writer, trace_id=new_trace_id()
            )
            dump_yaml_path(merge_annotation(raw, annotation), path)
            return WorkProduct(value=path, usage=response.usage, confidence=annotation.confidence)

        return WorkUnit(
            id=f"annotate:{path.name}",
            run=run,
            kind="annotate",
            label=path.name,
            primes_cache=prime,
        )

    # The first unit primes the shared schema/semantics prompt cache for the rest.
    units = [make(p, prime=(i == 0)) for i, p in enumerate(paths)]
    remaining = meter.remaining
    result = orchestrator.run(units, budget=remaining, price=lambda u: price_usage(u, model))

    annotated = 0
    failures: list[tuple[str, str]] = []
    for outcome in result.outcomes:
        if outcome.ok and outcome.product is not None:
            meter.record(outcome.usage, model=model, role="annotator", run_id="init")
            _record_annotated(project, outcome.product.value)
            annotated += 1
        elif outcome.status.value == "failed":
            failures.append((outcome.unit.label, outcome.error or "unknown error"))
    return annotated, failures, result


def enrich(
    project: Project,
    plan: InitPlan,
    *,
    llm: LLMClient,
    meter: SpendMeter,
    orchestrator: Orchestrator,
    authorize: AuthorizeFn,
    schema_fingerprint: str = "",
    replay: str | Path | None = None,
    record: str | Path | None = None,
    as_of: datetime | None = None,
) -> EnrichmentResult:
    """Run the approved paid stages in order, gating each on the live budget.

    ``authorize`` decides whether the next stage may run given the meter and the stage's
    estimate (guided pause vs ``--auto`` hard-stop — the CLI owns that policy). A stage that
    isn't authorized stops the run cleanly, leaving whatever's done in place for a re-run.
    """
    from sqbyl.candidates_io import add_candidates
    from sqbyl.eval.benchmarks_io import append_to_dev_set
    from sqbyl_runtime.state.traces import TraceWriter

    result = EnrichmentResult()
    annotate_result: OrchestratorResult[Path] | None = None

    # 1. Annotate (parallel, budget-gated by the orchestrator itself).
    if plan.annotate_tables:
        est = annotate_estimate(plan.model, tables=len(plan.annotate_tables)).total_usd
        if not authorize(meter, est, f"annotate {len(plan.annotate_tables)} table(s)"):
            result.stopped = True
            return _finish(project, result, meter, annotate_result)
        annotated, failures, annotate_result = _annotate_wave(
            project, plan, llm=llm, meter=meter, orchestrator=orchestrator
        )
        result.annotated = annotated
        result.annotate_failures = failures
        # The orchestrator's own budget gate can leave later tables `skipped` — that's a
        # budget stop mid-wave, so halt the run (don't press on to synth/eval on a dry budget)
        # and flag it so the "re-run to continue" hint fires.
        if annotate_result.skipped:
            result.stopped = True
            return _finish(project, result, meter, annotate_result)

    # 2. Execution-grounded synth → auto-accept survivors into the dev set for a baseline.
    if plan.do_synth:
        from sqbyl.synth import synthesize

        est = synth_estimate(project.manifest.model.for_role("synth"), n=plan.synth_n).total_usd
        if not authorize(meter, est, f"synth ~{plan.synth_n} question(s)"):
            result.stopped = True
            return _finish(project, result, meter, annotate_result)
        synth_model = project.manifest.model.for_role("synth")
        synth = synthesize(
            project,
            llm=llm,
            model=synth_model,
            n=plan.synth_n,
            as_of=as_of,
            trace_writer=TraceWriter(SqbylPaths(project.root).ensure().traces_dir / "synth.jsonl"),
        )
        meter.record(synth.usage, model=synth_model, role="synth", run_id="init")
        add_candidates(project, synth.survivors)
        # Execution-grounded (gold SQL ran), so they seed the dev set; the human refines them
        # in `sqbyl review`. The gold is machine-authored and unreviewed, so the baseline over
        # them is provisional (surfaced as such by the CLI). Only dev is ever written —
        # test.yaml is untouched (invariant 3).
        append_to_dev_set(project, [c.to_question() for c in synth.survivors])
        result.survivors = synth.n_survivors

    # 3. Baseline eval on dev (meters itself to usage.db via project.eval).
    if plan.do_eval and _dev_question_count(project) > 0:
        est = _eval_estimate_total(project, plan)
        if not authorize(meter, est, "baseline eval"):
            result.stopped = True
            return _finish(project, result, meter, annotate_result)
        # Share the one injected client across the whole journey (so it runs end-to-end under
        # a single record-replay cassette); fall back to replay/record when none was injected.
        run = project.eval("dev", llm=llm, replay=replay, record=record, as_of=as_of)
        result.run = run
        # Stamp what this baseline ran against so an unchanged re-run skips re-evaluating, but
        # a changed schema or a different --as-of forces it.
        _record_baseline(project, schema_fingerprint=schema_fingerprint, as_of=as_of)

        # 4. Coach the failures (if automation is on), so the queue arrives pre-populated.
        if project.manifest.automation.auto_coach:
            result.coach_report = _maybe_coach(
                project, run, llm=llm, meter=meter, authorize=authorize
            )

    return _finish(project, result, meter, annotate_result)


def _eval_estimate_total(project: Project, plan: InitPlan) -> float:
    judge = (
        project.manifest.model.for_role("judge") if project.manifest.automation.auto_judge else None
    )
    return eval_estimate(
        plan.model,
        questions=max(plan.eval_questions, _dev_question_count(project)),
        judge_model=judge,
        self_repair_attempts=project.manifest.defaults.self_repair_attempts,
    ).total_usd


def _maybe_coach(
    project: Project,
    run: ScoredRun,
    *,
    llm: LLMClient,
    meter: SpendMeter,
    authorize: AuthorizeFn,
) -> CoachReport | None:
    from sqbyl.coach import coach, gather_failures, save_report
    from sqbyl.estimates import coach_estimate
    from sqbyl_runtime.state.traces import TraceWriter

    failures = gather_failures(run)
    if not failures:
        return None
    model = project.manifest.model.for_role("coach")
    est = coach_estimate(model, failures=len(failures)).total_usd
    if not authorize(meter, est, f"coach {len(failures)} failure(s)"):
        return None
    paths = SqbylPaths(project.root).ensure()
    report = coach(
        project,
        run,
        llm=llm,
        model=model,
        trace_writer=TraceWriter(paths.traces_dir / "coach.jsonl"),
    )
    meter.record(report.usage, model=model, role="coach", run_id="init")
    save_report(paths, report)
    return report


def _finish(
    project: Project,
    result: EnrichmentResult,
    meter: SpendMeter,
    annotate_result: OrchestratorResult[Path] | None,
) -> EnrichmentResult:
    """Assemble the leverage-sorted attention queue the user arrives at (spec §5.5)."""
    # The eval stage meters itself to usage.db (via project.eval), so its spend isn't on the
    # init meter — fold it in so the reported total reconciles with the ledger.
    eval_cost = result.run.total_cost_usd if result.run is not None else 0.0
    result.spent_usd = meter.spent + eval_cost
    result.queue = build_arrival_queue(project, result.run, result.coach_report, annotate_result)
    return result


def build_arrival_queue(
    project: Project,
    run: ScoredRun | None,
    report: CoachReport | None,
    annotate_result: OrchestratorResult[Path] | None,
) -> AttentionQueue:
    """Route the enrichment's outputs into the Phase 6 attention queue.

    Coach proposals, the eval review pile, and any failed-annotation cards become decisions;
    :func:`route` auto-applies the confident ones and leverage-sorts the rest against the
    readiness target. The dev run alone feeds accuracy — the held-out test is never here.
    """
    decisions = []
    total = run.total if run is not None else 0
    if report is not None and total:
        decisions += decisions_from_coach_report(report, total=total)
    if run is not None:
        decisions += decisions_from_review_pile(run)
    if annotate_result is not None:
        # The adapter only reads status/label/error; Path units are fine as object outcomes.
        decisions += decisions_from_outcomes(cast("OrchestratorResult[object]", annotate_result))

    n_correct = run.n_correct if run is not None else 0
    n = run.total if run is not None else 0
    return route(
        decisions,
        n_correct=n_correct,
        n=n,
        target=project.manifest.defaults.readiness_target,
        auto_apply_threshold=project.manifest.defaults.auto_apply_threshold,
    )
