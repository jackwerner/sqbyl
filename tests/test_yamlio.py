"""Phase 0.4 — the project YAML loader keeps join `on:` and yes/no values as strings."""

from __future__ import annotations

from sqbyl.yamlio import load_yaml


def test_on_key_is_a_string_not_boolean() -> None:
    data = load_yaml("on: a = b\n")
    assert data == {"on": "a = b"}  # not {True: ...}


def test_yes_no_stay_strings() -> None:
    data = load_yaml("sample_values: [yes, no, on, off]\n")
    assert data == {"sample_values": ["yes", "no", "on", "off"]}


def test_true_false_are_still_booleans() -> None:
    data = load_yaml("read_only: true\nprompt_caching: false\n")
    assert data == {"read_only": True, "prompt_caching": False}
