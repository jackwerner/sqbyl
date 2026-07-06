"""Phase 0.3 — the LLMClient seam: mock + record-replay, zero network.

These prove invariant 4 (mock-first; CI never spends tokens): a trivial caller is
unit-tested against the mock, and a cassette can be captured and replayed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from sqbyl_runtime.llm import (
    CassetteMissError,
    LLMRequest,
    Message,
    MockLLMClient,
    RecordReplayLLMClient,
    Usage,
    structured_reply,
    text_reply,
)


class Plan(BaseModel):
    plan: str
    sql: str


def _ask(client, text: str = "hi"):  # type: ignore[no-untyped-def]
    """A trivial caller exercised against any LLMClient impl."""
    return client.complete_text([Message(role="user", content=text)], model="claude-opus-4-8")


def test_mock_returns_scripted_text_with_usage() -> None:
    client = MockLLMClient([text_reply("hello", usage=Usage(input_tokens=12, output_tokens=3))])
    resp = _ask(client)
    assert resp.text == "hello"
    assert resp.usage.total_tokens == 15
    # The mock echoes the requested model when the script didn't pin one.
    assert resp.model == "claude-opus-4-8"
    assert client.call_count == 1
    assert client.requests[0].messages[0].content == "hi"


def test_mock_structured_output_parses_into_model() -> None:
    client = MockLLMClient([structured_reply({"plan": "count rows", "sql": "SELECT 1"})])
    parsed, resp = client.complete_structured(
        [Message(role="user", content="q")],
        model="claude-opus-4-8",
        response_model=Plan,
    )
    assert isinstance(parsed, Plan)
    assert parsed.sql == "SELECT 1"
    # The seam puts the requested schema on the request so record-replay keys on it.
    assert resp.structured == {"plan": "count rows", "sql": "SELECT 1"}
    assert client.requests[0].response_schema is not None


class Batch(BaseModel):
    questions: list[Plan]


def test_parse_recovers_from_stringified_nested_field() -> None:
    # Observed intermittently on opus/gpt for list-of-object schemas: the forced-tool
    # argument comes back with the list stuffed in as a JSON *string*. parse() must
    # decode it and validate rather than crashing the whole command.
    import json

    from sqbyl_runtime.llm import LLMResponse

    good = [{"plan": "a", "sql": "SELECT 1"}, {"plan": "b", "sql": "SELECT 2"}]
    resp = LLMResponse(model="claude-opus-4-8", structured={"questions": json.dumps(good)})
    parsed = resp.parse(Batch)
    assert isinstance(parsed, Batch)
    assert [q.sql for q in parsed.questions] == ["SELECT 1", "SELECT 2"]


def test_parse_still_raises_on_genuinely_bad_payload() -> None:
    from sqbyl_runtime.llm import LLMResponse

    resp = LLMResponse(model="claude-opus-4-8", structured={"questions": "not json at all"})
    with pytest.raises(Exception):  # noqa: B017  (pydantic ValidationError)
        resp.parse(Batch)


def test_mock_exhaustion_is_a_loud_error() -> None:
    client = MockLLMClient([text_reply("only one")])
    _ask(client)
    with pytest.raises(AssertionError, match="exhausted"):
        _ask(client)


def test_mock_callable_reply_can_depend_on_request() -> None:
    # First call returns bad SQL, second returns good — the self-repair test shape.
    def reply(req: LLMRequest):  # type: ignore[no-untyped-def]
        bad = "broken" in req.messages[-1].content
        return text_reply("SELECT bad" if bad else "SELECT 1")

    client = MockLLMClient([reply, reply])
    assert _ask(client, "broken").text == "SELECT bad"
    assert _ask(client, "fixed").text == "SELECT 1"


def test_request_fingerprint_is_stable_and_discriminating() -> None:
    a = LLMRequest(model="m", messages=[Message(role="user", content="x")])
    b = LLMRequest(model="m", messages=[Message(role="user", content="x")])
    c = LLMRequest(model="m", messages=[Message(role="user", content="y")])
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != c.fingerprint()


def test_record_then_replay_roundtrip(tmp_path: Path) -> None:
    cassette = tmp_path / "cassette.json"

    # "record" against a mock standing in for the real client — proves the machinery
    # without any network.
    backend = MockLLMClient([text_reply("recorded answer", usage=Usage(input_tokens=7))])
    recorder = RecordReplayLLMClient(cassette, mode="record", inner=backend)
    first = _ask(recorder, "what is revenue?")
    assert first.text == "recorded answer"
    assert cassette.exists()

    # A fresh replay client (no inner, no network) returns the captured response.
    player = RecordReplayLLMClient(cassette, mode="replay")
    replayed = _ask(player, "what is revenue?")
    assert replayed.text == "recorded answer"
    assert replayed.usage.input_tokens == 7


def test_replay_miss_raises(tmp_path: Path) -> None:
    cassette = tmp_path / "empty.json"
    cassette.write_text('{"version": 1, "entries": {}}\n')
    player = RecordReplayLLMClient(cassette, mode="replay")
    with pytest.raises(CassetteMissError):
        _ask(player, "never recorded")


def test_record_mode_requires_inner(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="inner client"):
        RecordReplayLLMClient(tmp_path / "c.json", mode="record")
