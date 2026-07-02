"""Phase 6.1 — the orchestrator engine (spec §3 #8).

The three headline behaviors from the plan's "done when":
  * a simulated 429 triggers backoff (not failure);
  * a deliberately-failing unit degrades to a card while its siblings complete;
  * cache-prime ordering is enforced (the primer finishes before the wave starts).

Plus budget-capped dispatch (invariant 5) and a real-workload integration proof that the
engine drives concurrent LLM-seam calls correctly (via an order-independent replay cassette).
"""

from __future__ import annotations

import json
import os
import random
import threading
from pathlib import Path

import pytest

from sqbyl.annotate import TableAnnotation, annotate_table
from sqbyl.introspect import introspect
from sqbyl.orchestrator import (
    EventKind,
    Orchestrator,
    OrchestratorEvent,
    UnitStatus,
    WorkProduct,
    WorkUnit,
)
from sqbyl.profile import profile_table
from sqbyl_runtime.db import Database
from sqbyl_runtime.llm.base import LLMResponse, RateLimitError, Usage
from sqbyl_runtime.llm.mock import MockLLMClient
from sqbyl_runtime.llm.replay import RecordReplayLLMClient
from sqbyl_runtime.models import Dialect, TableSemantics


def _unit(uid: str, value: object = None, *, tokens: int = 10) -> WorkUnit[object]:
    """A trivially-succeeding unit that reports ``tokens`` input tokens."""

    def run() -> WorkProduct[object]:
        return WorkProduct(
            value=value if value is not None else uid, usage=Usage(input_tokens=tokens)
        )

    return WorkUnit(id=uid, run=run, label=uid)


def _noop_sleep(_: float) -> None:
    pass


# ── happy path ────────────────────────────────────────────────────────────────────────


def test_runs_all_units_preserving_order_and_summing_usage() -> None:
    orch = Orchestrator(concurrency=4)
    units = [_unit(f"u{i}", tokens=i + 1) for i in range(5)]

    result = orch.run(units)

    assert [o.unit.id for o in result.outcomes] == ["u0", "u1", "u2", "u3", "u4"]
    assert all(o.ok for o in result.outcomes)
    assert result.total_usage.input_tokens == 1 + 2 + 3 + 4 + 5
    assert [o.value for o in result.outcomes] == ["u0", "u1", "u2", "u3", "u4"]


# ── 429 backoff (not failure) ───────────────────────────────────────────────────────────


def test_rate_limit_backs_off_then_succeeds() -> None:
    calls = {"n": 0}

    def run() -> WorkProduct[str]:
        calls["n"] += 1
        if calls["n"] <= 2:  # 429 on the first two attempts, then succeed
            raise RateLimitError("slow down")
        return WorkProduct(value="ok", usage=Usage(input_tokens=7))

    sleeps: list[float] = []
    orch = Orchestrator(
        concurrency=1,
        base_backoff=1.0,
        sleep=sleeps.append,
        rng=random.Random(0),  # deterministic jitter
    )
    result = orch.run([WorkUnit(id="u", run=run)])

    outcome = result.outcomes[0]
    assert outcome.status is UnitStatus.ok
    assert outcome.attempts == 3
    assert len(sleeps) == 2  # backed off twice, did not fail
    assert result.total_usage.input_tokens == 7


def test_rate_limit_honors_server_retry_after() -> None:
    def run() -> WorkProduct[str]:
        raise RateLimitError("slow down", retry_after=4.0)

    sleeps: list[float] = []
    orch = Orchestrator(concurrency=1, max_retries=1, max_backoff=30.0, sleep=sleeps.append)
    result = orch.run([WorkUnit(id="u", run=run)])

    assert result.outcomes[0].status is UnitStatus.failed  # retries exhausted
    assert sleeps == [4.0]  # honored the server hint, not the exponential guess


def test_retry_usage_is_metered_not_dropped() -> None:
    # A 429 that carries usage (billed input) then a success: the outcome meters BOTH.
    calls = {"n": 0}

    def run() -> WorkProduct[str]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RateLimitError("billed 429", usage=Usage(input_tokens=100))
        return WorkProduct(value="ok", usage=Usage(input_tokens=50))

    orch = Orchestrator(concurrency=1, sleep=_noop_sleep)
    result = orch.run([WorkUnit(id="u", run=run)])

    assert result.outcomes[0].usage.input_tokens == 150  # 100 (429) + 50 (success)
    assert result.total_usage.input_tokens == 150


def test_rate_limit_exhausts_retries_and_degrades_to_a_card() -> None:
    def run() -> WorkProduct[str]:
        raise RateLimitError("always")

    orch = Orchestrator(concurrency=1, max_retries=3, sleep=_noop_sleep)
    result = orch.run([WorkUnit(id="u", run=run, label="stubborn")])

    outcome = result.outcomes[0]
    assert outcome.status is UnitStatus.failed
    assert outcome.attempts == 4  # initial + 3 retries
    assert outcome.error is not None and "rate-limited" in outcome.error


# ── partial-failure tolerance ───────────────────────────────────────────────────────────


def test_failing_unit_degrades_to_a_card_while_siblings_complete() -> None:
    def boom() -> WorkProduct[str]:
        raise ValueError("cryptic column")

    units: list[WorkUnit[object]] = [
        _unit("a"),
        WorkUnit(id="b", run=boom, label="table b"),
        _unit("c"),
    ]
    orch = Orchestrator(concurrency=3)
    result = orch.run(units)

    assert {o.unit.id for o in result.ok} == {"a", "c"}
    assert [o.unit.id for o in result.failures] == ["b"]
    failed = result.failures[0]
    assert failed.error is not None and "ValueError: cryptic column" in failed.error
    assert failed.value is None  # nothing to apply — the router makes it a low-confidence card


# ── cache-prime ordering ────────────────────────────────────────────────────────────────


def test_cache_priming_unit_finishes_before_the_wave_starts() -> None:
    events: list[OrchestratorEvent] = []
    lock = threading.Lock()

    def record(e: OrchestratorEvent) -> None:
        with lock:
            events.append(e)

    primer = WorkUnit(id="prime", run=lambda: WorkProduct(value="warm"), primes_cache=True)
    wave = [_unit(f"w{i}") for i in range(4)]

    orch = Orchestrator(concurrency=4, on_event=record)
    orch.run([*wave, primer])  # primer listed last on purpose

    prime_done = next(
        i for i, e in enumerate(events) if e.unit_id == "prime" and e.kind is EventKind.succeeded
    )
    first_wave_start = min(
        i for i, e in enumerate(events) if e.unit_id.startswith("w") and e.kind is EventKind.started
    )
    assert prime_done < first_wave_start


# ── budget awareness (invariant 5) ──────────────────────────────────────────────────────


def test_budget_leaves_remaining_units_skipped() -> None:
    # Each unit costs $1 (1000 input tokens priced at $1/1k here); a $2 budget admits ~2.
    def price(usage: Usage) -> float:
        return usage.input_tokens / 1000.0

    units = [_unit(f"u{i}", tokens=1000) for i in range(6)]
    orch = Orchestrator(concurrency=1)  # serial so the cap trips deterministically
    result = orch.run(units, budget=2.0, price=price)

    statuses = [o.status for o in result.outcomes]
    assert statuses.count(UnitStatus.ok) == 2
    assert statuses.count(UnitStatus.skipped) == 4
    assert result.total_cost_usd == pytest.approx(2.0)


def test_duplicate_unit_ids_are_rejected() -> None:
    # Outcomes are keyed by id and re-ordered by it; a duplicate would silently collapse.
    units = [_unit("dup"), _unit("dup")]
    with pytest.raises(ValueError, match="unique"):
        Orchestrator().run(units)


def test_no_budget_runs_everything() -> None:
    units = [_unit(f"u{i}", tokens=1000) for i in range(6)]
    result = Orchestrator(concurrency=2).run(units, price=lambda u: u.input_tokens / 1000.0)
    assert all(o.ok for o in result.outcomes)


# ── real-workload integration: the engine drives concurrent LLM-seam calls ──────────────

_CASSETTE = Path(__file__).resolve().parent / "cassettes" / "annotate_orders.json"
_RESPONSE = LLMResponse(
    model="claude-opus-4-8",
    structured={
        "description": "One row per order placed by a customer.",
        "synonyms": ["purchases", "sales"],
        "confidence": 0.92,
        "columns": [
            {"name": "amount_cents", "description": "Order total in cents.", "confidence": 0.95},
        ],
    },
    stop_reason="tool_use",
    usage=Usage(input_tokens=900, output_tokens=120, cache_creation_input_tokens=800),
)


@pytest.fixture
def profiled_orders(duckdb_path: Path) -> TableSemantics:
    with Database.connect(str(duckdb_path), dialect=Dialect.duckdb) as db:
        orders = next(t for t in introspect(db) if t.table == "analytics.orders")
        return profile_table(db, orders)


def _ensure_cassette(table: TableSemantics) -> None:
    if _CASSETTE.exists() and not os.environ.get("SQBYL_UPDATE_CASSETTES"):
        return
    capture = MockLLMClient([_RESPONSE])
    annotate_table(capture, table, model="claude-opus-4-8")
    request = capture.requests[0]
    payload = {
        "version": 1,
        "entries": {
            request.fingerprint(): {
                "request": request.model_dump(mode="json"),
                "response": _RESPONSE.model_dump(mode="json"),
            }
        },
    }
    _CASSETTE.parent.mkdir(parents=True, exist_ok=True)
    _CASSETTE.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def test_orchestrates_concurrent_annotate_units_via_replay(profiled_orders: TableSemantics) -> None:
    """The engine fans real annotate work across threads through the (replay) LLM seam.

    The replay client is fingerprint-keyed, so concurrent access is order-independent — this
    proves the pool drives genuine paid closures, not just toy lambdas, and sums their usage.
    """
    _ensure_cassette(profiled_orders)
    client = RecordReplayLLMClient(_CASSETTE, mode="replay")

    def make(i: int) -> WorkUnit[TableAnnotation]:
        def run() -> WorkProduct[TableAnnotation]:
            annotation, response = annotate_table(client, profiled_orders, model="claude-opus-4-8")
            return WorkProduct(
                value=annotation, usage=response.usage, confidence=annotation.confidence
            )

        return WorkUnit(id=f"annotate-{i}", run=run, kind="annotate", primes_cache=(i == 0))

    units = [make(i) for i in range(4)]
    result = Orchestrator(concurrency=3).run(units)

    assert all(o.ok for o in result.outcomes)
    assert all(isinstance(o.value, TableAnnotation) for o in result.outcomes)
    assert all(o.confidence == 0.92 for o in result.outcomes)
    assert result.total_usage.input_tokens == 900 * 4
