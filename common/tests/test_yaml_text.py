"""`common.deploy.yaml_text` is the one YAML parse cache of the repository (#1602).

`test_gate_yaml_parse_cache.py` proves the key, copy and failure rules through the
merge gate. This file proves the non-str rule, that the gate and four deploy modules
import the same function, and that no module of `common/deploy` or `common/gate`
calls `yaml.safe_load` directly any more.
"""

from __future__ import annotations

import ast
import io
from pathlib import Path

import pytest
import yaml

from common.deploy import config_hash, contract, dependencies, in_service, yaml_text
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


def test_bytes_are_parsed_on_every_call() -> None:
    loader = _Counting()
    first = yaml_text.safe_load_text(_Module(loader), (COMPOSE + "# bytes\n").encode())
    second = yaml_text.safe_load_text(_Module(loader), (COMPOSE + "# bytes\n").encode())
    assert first == second == {"services": {"app": {"image": "example/app:1"}}}
    assert loader.calls == 2


def test_a_stream_is_read_again_not_answered_from_the_cache() -> None:
    # The same stream object twice: the second read finds it exhausted, as with
    # yaml.safe_load. A cache keyed on the stream object would return the first value.
    loader = _Counting()
    stream = io.StringIO(COMPOSE + "# stream\n")
    first = yaml_text.safe_load_text(_Module(loader), stream)
    second = yaml_text.safe_load_text(_Module(loader), stream)
    assert first == {"services": {"app": {"image": "example/app:1"}}}
    assert second is None
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


ROOT = Path(__file__).resolve().parents[2]


def _direct_safe_load_calls(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "safe_load"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "yaml"
    ]


def test_no_deploy_or_gate_module_calls_yaml_safe_load_directly() -> None:
    files = sorted((ROOT / "common" / "deploy").glob("*.py")) + sorted(
        (ROOT / "common" / "gate").glob("*.py")
    )
    assert len(files) >= 10, "the scan found too few modules"
    direct = {
        f.relative_to(ROOT).as_posix(): lines
        for f in files
        if (lines := _direct_safe_load_calls(f))
    }
    assert not direct, f"parse through common.deploy.yaml_text.safe_load_text: {direct}"
