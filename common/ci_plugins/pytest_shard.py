"""Pytest plugin that runs one deterministic shard of the collected tests (#1499).

Usage: ``pytest -p common.core.pytest_shard --shard=K/N`` (K is 1-based).
A test belongs to shard K when ``crc32(node id) % N == K - 1``. The rule uses
only the node id, so every pytest-xdist worker computes the same split.
Without ``--shard`` the plugin changes nothing.
"""

from __future__ import annotations

import zlib

import pytest


def parse_shard(value: str) -> tuple[int, int]:
    """Return (index, count) from ``K/N``; raise ValueError on bad input."""
    try:
        index_text, count_text = value.split("/")
        index, count = int(index_text), int(count_text)
    except ValueError:
        raise ValueError(f"--shard must look like K/N, got {value!r}") from None
    if count < 1 or not 1 <= index <= count:
        raise ValueError(f"--shard needs 1 <= K <= N, got {value!r}")
    return index, count


def shard_of(nodeid: str, count: int) -> int:
    """Return the 1-based shard that owns ``nodeid``."""
    return zlib.crc32(nodeid.encode()) % count + 1


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--shard",
        action="store",
        default=None,
        metavar="K/N",
        help="Run only shard K of N (1-based), chosen by a hash of the node id.",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    value = config.getoption("--shard")
    if value is None:
        return
    index, count = parse_shard(value)
    kept = [i for i in items if shard_of(i.nodeid, count) == index]
    dropped = [i for i in items if shard_of(i.nodeid, count) != index]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = kept
