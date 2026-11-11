"""The merge gate parses each distinct YAML text once per process (#1602).

`common.gate.types.safe_load_text` keys its cache on the `safe_load` function and
the text. These tests prove the four properties that keep the cache safe: one
parse per pair, a new parse for a new loader, no stored failure, and a private
copy for each caller.
"""

from __future__ import annotations

import types

import pytest
import yaml

from common.gate import inventory
from common.gate.types import safe_load_text

TEXT = "gates:\n  - id: a\n    blocks_merge: true\n"


class _CountingLoader:
    """A `safe_load` that counts its calls and delegates to PyYAML."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, text: str) -> object:
        self.calls += 1
        return yaml.safe_load(text)


def _module(loader) -> types.SimpleNamespace:
    return types.SimpleNamespace(safe_load=loader, YAMLError=yaml.YAMLError)


def test_the_same_text_is_parsed_once_per_loader() -> None:
    loader = _CountingLoader()
    first = safe_load_text(_module(loader), TEXT + "# once\n")
    second = safe_load_text(_module(loader), TEXT + "# once\n")
    assert first == second == {"gates": [{"id": "a", "blocks_merge": True}]}
    assert loader.calls == 1


def test_a_different_text_is_parsed_again() -> None:
    loader = _CountingLoader()
    safe_load_text(_module(loader), TEXT + "# x\n")
    safe_load_text(_module(loader), TEXT + "# y\n")
    assert loader.calls == 2


def test_a_replaced_loader_never_gets_the_result_of_another_loader() -> None:
    real = _CountingLoader()
    safe_load_text(_module(real), TEXT + "# replaced\n")

    def broken(text: str) -> object:
        raise yaml.YAMLError("simulated parse failure")

    with pytest.raises(yaml.YAMLError, match="simulated"):
        safe_load_text(_module(broken), TEXT + "# replaced\n")


def test_a_failed_parse_is_not_stored() -> None:
    attempts: list[str] = []

    def flaky(text: str) -> object:
        attempts.append(text)
        if len(attempts) == 1:
            raise yaml.YAMLError("first attempt fails")
        return yaml.safe_load(text)

    with pytest.raises(yaml.YAMLError):
        safe_load_text(_module(flaky), TEXT + "# flaky\n")
    assert safe_load_text(_module(flaky), TEXT + "# flaky\n") == {
        "gates": [{"id": "a", "blocks_merge": True}]
    }
    assert len(attempts) == 2


def test_a_caller_that_changes_its_result_does_not_change_the_next_result() -> None:
    loader = _CountingLoader()
    first = safe_load_text(_module(loader), TEXT + "# copy\n")
    first["gates"][0]["blocks_merge"] = False
    first["extra"] = 1
    second = safe_load_text(_module(loader), TEXT + "# copy\n")
    assert second == {"gates": [{"id": "a", "blocks_merge": True}]}
    assert loader.calls == 1


def test_an_unhashable_loader_parses_every_call() -> None:
    class Unhashable:
        __hash__ = None  # type: ignore[assignment]

        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, text: str) -> object:
            self.calls += 1
            return yaml.safe_load(text)

    loader = Unhashable()
    safe_load_text(_module(loader), TEXT)
    safe_load_text(_module(loader), TEXT)
    assert loader.calls == 2


def test_the_required_checks_read_parses_each_file_once(monkeypatch) -> None:
    # `_required_checks` reads the inventory and one workflow per blocking gate. Before
    # #1602 every blocking gate of infra-ci.yml parsed that file again.
    loader = _CountingLoader()
    monkeypatch.setattr(inventory, "_get_yaml", lambda: _module(loader))
    inventory._required_checks.cache_clear()
    try:
        names, read = inventory._required_checks()
        first_calls = loader.calls
        inventory._required_checks.cache_clear()
        assert inventory._required_checks() == (names, read)
    finally:
        inventory._required_checks.cache_clear()
    assert read and "Test Deployer Hash Logic" in names
    gates = yaml.safe_load(
        (inventory._get_root() / inventory.INVENTORY).read_text(encoding="utf-8")
    )["gates"]
    distinct_files = 1 + len({g["workflow"] for g in gates if g.get("blocks_merge")})
    assert first_calls == distinct_files
    assert loader.calls == first_calls, "the second read must not parse again"
