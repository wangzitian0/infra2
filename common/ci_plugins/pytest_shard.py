"""Pytest plugin that runs one deterministic shard of the test files (#1499, #1602).

Usage: ``PYTHONPATH=<repo>/common/ci_plugins pytest -p pytest_shard --shard=K/N`` (K is 1-based).
The directory goes on PYTHONPATH because PYTHONSAFEPATH=1 hides the working directory.
Without ``--shard`` the plugin changes nothing.

Each test file belongs to one shard. The plugin assigns the files before collection,
so a worker imports only the test files of its own shard. Before #1602 each
pytest-xdist worker collected every test of the tree and dropped the tests of the
other shards: 12 full collections for 3 shards of 4 workers.

Assignment rule (longest processing time first):

1. The weight of a file is its measured seconds in ``shard_weights.json``.
   A file with no weight gets the median of the known weights (1.0 when none is known).
2. Sort the files by weight, largest first, then by path.
3. Put each file in the shard with the lowest total weight. A tie goes to the lowest K.

The rule reads only file paths and the weights file, so every process computes the
same split. A wrong or old weight changes only the balance, never the partition.

Measure the weights on CI, not on a laptop: CPU-bound and process-bound tests scale
differently on the 4 vCPU runner. Each CI shard writes its file seconds with
``--shard-durations-out``; ``test-deployer-logic`` merges them and uploads the
artifact ``infra2-shard-weights``. Copy that file to ``common/ci_plugins/shard_weights.json``
when the printed shard loads drift apart. Local merge::

    python common/ci_plugins/pytest_shard.py merge OUT.json durations.shard*.json
"""

from __future__ import annotations

import fnmatch
import json
import statistics
import sys
import zlib
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

import pytest

DEFAULT_WEIGHTS = Path(__file__).with_name("shard_weights.json")
_ASSIGNMENT = pytest.StashKey[dict[str, int]]()


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


def load_weights(path: Path) -> dict[str, float]:
    """Return ``{file: seconds}`` from ``path``; an absent file gives no weights."""
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: the shard weights must be a JSON object")
    weights: dict[str, float] = {}
    for name, seconds in data.items():
        if not isinstance(seconds, (int, float)) or seconds < 0:
            raise ValueError(f"{path}: weight of {name!r} must be seconds >= 0")
        weights[str(name)] = float(seconds)
    return weights


def assign(
    files: Iterable[str], weights: Mapping[str, float], count: int
) -> dict[str, int]:
    """Return ``{file: shard}`` (1-based) by the assignment rule of this module."""
    unique = sorted(set(files))
    known = [weights[f] for f in unique if f in weights]
    default = statistics.median(known) if known else 1.0
    weight = {f: weights.get(f, default) for f in unique}
    loads = [0.0] * count
    shard: dict[str, int] = {}
    for name in sorted(unique, key=lambda f: (-weight[f], f)):
        lowest = min(range(count), key=lambda k: (loads[k], k))
        loads[lowest] += weight[name]
        shard[name] = lowest + 1
    return shard


def _key(path: Path, config: pytest.Config) -> str:
    """Return the rootdir-relative POSIX path of ``path`` (absolute when outside)."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(config.rootpath.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _is_test_file(path: Path, patterns: Sequence[str]) -> bool:
    return path.suffix == ".py" and any(fnmatch.fnmatch(path.name, p) for p in patterns)


def _test_files(config: pytest.Config) -> list[str]:
    """Return every test file below the collection arguments, as rootdir keys."""
    patterns = list(config.getini("python_files"))
    base = Path(config.invocation_params.dir)
    found: set[str] = set()
    for arg in config.args:
        target = base / arg.split("::", 1)[0]
        if target.is_file():
            found.add(_key(target, config))
        elif target.is_dir():
            for path in target.rglob("*.py"):
                if "__pycache__" not in path.parts and _is_test_file(path, patterns):
                    found.add(_key(path, config))
    return sorted(found)


def _shard_of(path: Path, config: pytest.Config, count: int) -> int:
    assignment = config.stash.get(_ASSIGNMENT, None)
    if assignment is None:
        weights_path = Path(config.getoption("--shard-weights"))
        weights = load_weights(weights_path)
        assignment = assign(_test_files(config), weights, count)
        config.stash[_ASSIGNMENT] = assignment
    key = _key(path, config)
    if key in assignment:
        return assignment[key]
    # A file outside the collection arguments: a stable hash keeps the partition.
    return zlib.crc32(key.encode()) % count + 1


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--shard",
        action="store",
        default=None,
        metavar="K/N",
        help="Run only the test files of shard K of N (1-based).",
    )
    parser.addoption(
        "--shard-weights",
        action="store",
        default=str(DEFAULT_WEIGHTS),
        metavar="PATH",
        help="JSON object {test file: seconds} that balances the shards.",
    )
    parser.addoption(
        "--shard-durations-out",
        action="store",
        default=None,
        metavar="PATH",
        help="Write the measured seconds of each test file to PATH as JSON.",
    )


def pytest_configure(config: pytest.Config) -> None:
    value = config.getoption("--shard")
    if value is not None:
        try:
            parse_shard(value)
        except ValueError as exc:
            raise pytest.UsageError(str(exc)) from None
    out = config.getoption("--shard-durations-out")
    # An xdist worker forwards its reports to the controller; only the controller writes.
    if out is not None and not hasattr(config, "workerinput"):
        config.pluginmanager.register(_DurationRecorder(Path(out)), "shard-durations")


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    value = config.getoption("--shard")
    if value is None:
        return None
    if not collection_path.is_file():
        return None
    if not _is_test_file(collection_path, list(config.getini("python_files"))):
        return None
    index, count = parse_shard(value)
    return True if _shard_of(collection_path, config, count) != index else None


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    # Backstop for a test file named on the command line: pytest does not ask
    # pytest_ignore_collect about such a path, so drop its items here.
    value = config.getoption("--shard")
    if value is None:
        return
    index, count = parse_shard(value)
    kept = [i for i in items if _shard_of(i.path, config, count) == index]
    dropped = [i for i in items if _shard_of(i.path, config, count) != index]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = kept


class _DurationRecorder:
    """Sum the setup, call and teardown seconds of each test file and write them."""

    def __init__(self, out: Path) -> None:
        self.out = out
        self.seconds: dict[str, float] = defaultdict(float)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self.seconds[report.nodeid.split("::", 1)[0]] += report.duration

    def pytest_sessionfinish(self) -> None:
        payload = {f: round(s, 2) for f, s in sorted(self.seconds.items())}
        self.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def merge_durations(paths: Sequence[Path]) -> dict[str, float]:
    """Return the per-file seconds of ``paths`` summed into one sorted mapping."""
    merged: dict[str, float] = {}
    for path in paths:
        for name, seconds in load_weights(path).items():
            merged[name] = round(merged.get(name, 0.0) + seconds, 2)
    return dict(sorted(merged.items()))


def main(argv: Sequence[str] | None = None) -> int:
    """``merge OUT IN...``: print the load of each IN and write their sum to OUT."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[0] != "merge":
        print(
            "usage: pytest_shard.py merge OUT.json DURATIONS.json...", file=sys.stderr
        )
        return 2
    out, inputs = Path(args[1]), [Path(a) for a in args[2:]]
    missing = [str(p) for p in inputs if not p.is_file()]
    if missing:
        print(
            f"pytest_shard merge: missing input {', '.join(missing)}", file=sys.stderr
        )
        return 1
    for path in inputs:
        seconds = load_weights(path)
        print(f"{path.name}: {len(seconds)} files, {sum(seconds.values()):.1f} s")
    merged = merge_durations(inputs)
    out.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    print(f"merged {len(merged)} files, {sum(merged.values()):.1f} s into {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
