"""Each tracked compose file has no duplicate mapping key.

Docker Compose reads YAML with go-yaml v3, which refuses a duplicate key. PyYAML
``safe_load`` keeps the last value with no error, so the other compose tests cannot see
a duplicate. #1986 head 75cb7c4 set ``CONFIGURATION_SHA256`` twice in one
``environment`` block of platform/12.alerting/compose.yaml.

### Mutation evidence
- Adding a second ``CONFIGURATION_SHA256`` line to the alerting ``environment`` block
  fails ``test_no_duplicate_key[platform/12.alerting/compose.yaml]``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from common.deploy.compose_equivalence import tracked_compose_files

ROOT = Path(__file__).resolve().parents[2]


class _StrictLoader(yaml.SafeLoader):
    pass


def _strict_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False):
    seen: dict[object, int] = {}
    for key_node, _value in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None,
                None,
                f"duplicate key {key!r} (first at line {seen[key] + 1})",
                key_node.start_mark,
            )
        seen[key] = key_node.start_mark.line
    return loader.construct_mapping(node, deep=deep)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _strict_mapping
)


def duplicate_key_error(text: str) -> str:
    try:
        yaml.load(text, Loader=_StrictLoader)
    except yaml.constructor.ConstructorError as exc:
        return str(exc)
    return ""


def test_scope_is_not_empty() -> None:
    assert len(tracked_compose_files()) >= 20


@pytest.mark.parametrize("path", tracked_compose_files())
def test_no_duplicate_key(path: str) -> None:
    assert duplicate_key_error((ROOT / path).read_text(encoding="utf-8")) == ""


def test_the_loader_refuses_a_duplicate_and_allows_a_merge_key() -> None:
    dup = "services:\n  a:\n    environment:\n      X: '1'\n      X: '2'\n"
    assert "duplicate key 'X' (first at line 4)" in duplicate_key_error(dup)
    merge = "x: &b\n  K: '1'\nservices:\n  a:\n    <<: *b\n    environment: {}\n"
    assert duplicate_key_error(merge) == ""
