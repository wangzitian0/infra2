"""No workflow mapping repeats a key (#1133).

PyYAML keeps the last of two equal keys without a word. GitHub rejects the file. When a
`- name:` line is removed, two adjacent `run`-only steps merge into one mapping with two
`run:` keys: valid for PyYAML, invalid for GitHub. The step-shape test cannot see it,
because the first `run` is already gone when it reads the step.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((ROOT / ".github/workflows").glob("*.y*ml"))


class _NoDuplicateKeysLoader(yaml.SafeLoader):
    """SafeLoader that raises on a mapping with a repeated key."""


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep=False):
    seen: set = set()
    for key_node, _ in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                "while reading a mapping",
                node.start_mark,
                f"found the key {key!r} twice",
                key_node.start_mark,
            )
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_NoDuplicateKeysLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def _load(text: str):
    return yaml.load(text, Loader=_NoDuplicateKeysLoader)


def test_the_loader_rejects_two_run_keys_in_one_step() -> None:
    merged = "steps:\n  - name: a\n    run: echo a\n    run: echo b\n"
    with pytest.raises(yaml.constructor.ConstructorError, match="'run' twice"):
        _load(merged)


def test_the_loader_accepts_the_same_key_in_two_steps() -> None:
    steps = _load("steps:\n  - run: echo a\n  - run: echo b\n")
    assert [step["run"] for step in steps["steps"]] == ["echo a", "echo b"]


def test_no_workflow_repeats_a_key() -> None:
    assert len(WORKFLOWS) >= 5, "the scan reads too few workflows"
    for path in WORKFLOWS:
        try:
            _load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            pytest.fail(f"{path.name}: {error}")
