"""`common.yaml_text` is the one YAML parse cache of the repository (#1602).

`test_gate_yaml_parse_cache.py` proves the key, copy and failure rules through the
merge gate. This file proves the rules that only the shared module has, and that the
deploy modules parse through it.
"""

from __future__ import annotations

import io

import pytest
import yaml

from common.core import yaml_text
from common.deploy import config_hash, contract, dependencies, in_service
from common.gate import types as gate_types

COMPOSE = "services:\n  app:\n    image: example/app:1\n"


REAL_SAFE_LOAD = yaml.safe_load


class _Counting:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, text):
        self.calls += 1
        return REAL_SAFE_LOAD(text)


class _Module:
    def __init__(self, loader) -> None:
        self.safe_load = loader
        self.YAMLError = yaml.YAMLError


def test_the_gate_and_the_deploy_modules_share_one_parse_cache() -> None:
    assert gate_types.safe_load_text is yaml_text.safe_load_text
    for module in (config_hash, contract, dependencies, in_service):
        assert module.safe_load_text is yaml_text.safe_load_text, module.__name__


@pytest.mark.parametrize("make", [lambda t: t.encode(), io.StringIO])
def test_input_that_is_not_a_str_is_parsed_on_every_call(make) -> None:
    loader = _Counting()
    first = yaml_text.safe_load_text(_Module(loader), make(COMPOSE + "# bytes\n"))
    second = yaml_text.safe_load_text(_Module(loader), make(COMPOSE + "# bytes\n"))
    assert first == second == {"services": {"app": {"image": "example/app:1"}}}
    assert loader.calls == 2


def test_the_compose_parse_of_config_hash_reads_each_text_once(monkeypatch) -> None:
    loader = _Counting()
    monkeypatch.setattr(yaml, "safe_load", loader)
    text = COMPOSE + "# config hash\n"
    first = config_hash.parse_compose_services(text)
    first["app"]["image"] = "changed"
    second = config_hash.parse_compose_services(text)
    assert second == {"app": {"image": "example/app:1"}}
    assert loader.calls == 1


def test_a_replaced_safe_load_reaches_the_deploy_modules(monkeypatch) -> None:
    # A loader that a test installs must run, not a parse cached for the real loader.
    config_hash.parse_compose_services(COMPOSE + "# replaced\n")

    def broken(text):
        raise yaml.YAMLError("simulated")

    monkeypatch.setattr(yaml, "safe_load", broken)
    with pytest.raises(ValueError, match="simulated"):
        config_hash.parse_compose_services(COMPOSE + "# replaced\n")
