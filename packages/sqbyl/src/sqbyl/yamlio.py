"""YAML loading for sqbyl project files.

PyYAML implements YAML 1.1, which resolves bare ``on``/``off``/``yes``/``no`` as
booleans. sqbyl's semantic layer uses ``on:`` as the join-condition key (spec §4),
so a naive ``safe_load`` would turn that key into ``True``. ``SqbylYamlLoader``
restricts implicit boolean resolution to ``true``/``false`` only, so join ``on:``
and any ``yes``/``no`` sample values stay strings — matching what a human wrote.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


class SqbylYamlLoader(yaml.SafeLoader):
    """SafeLoader that does not treat on/off/yes/no as booleans."""


# Drop PyYAML's bool resolver and re-register one matching only true/false.
_STRICT_BOOL = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")


def _install_strict_bool_resolver() -> None:
    for ch in list(SqbylYamlLoader.yaml_implicit_resolvers):
        resolvers = [
            (tag, regexp)
            for tag, regexp in SqbylYamlLoader.yaml_implicit_resolvers[ch]
            if tag != "tag:yaml.org,2002:bool"
        ]
        SqbylYamlLoader.yaml_implicit_resolvers[ch] = resolvers
    SqbylYamlLoader.add_implicit_resolver(  # type: ignore[no-untyped-call]
        "tag:yaml.org,2002:bool", _STRICT_BOOL, list("tTfF")
    )


_install_strict_bool_resolver()


def load_yaml(source: str | Path) -> Any:
    """Parse YAML text or a file path with sqbyl's loader."""
    text = source.read_text() if isinstance(source, Path) else source
    return yaml.load(text, Loader=SqbylYamlLoader)
