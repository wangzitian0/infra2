#!/usr/bin/env python3
"""Every compose service must declare a real memory ceiling; the debt may only shrink.

`docs/ssot/ops.standards.md` §5 makes this a 必須 and was written from a measured
incident -- "2026-06 prefect 占 3G、零限额下内存 27/31G、Dokploy 控制面超时" --
with a blacklist adding "禁止无限额服务上 prod". Nothing checked it.

The first version of this lint was audited blind before it merged, and 10 of 12
mutations to it survived. Three findings shaped what it does now:

- It tested that the *key* was present, not that the *value* was a limit. Docker
  reads `mem_limit: 0` as no limit at all, so one line was enough to leave the
  ratchet forever while changing nothing. Values are parsed now.
- The ratchet recorded *files*. Adding a new unlimited service to one of the 21
  grandfathered files passed -- and those files are exactly the ones named in
  the incident. The baseline records `path::service`, so an existing file can no
  longer grow a new unlimited service.
- "Cannot parse" and "has no ceiling" were the same answer, so `include:`,
  `extends:` and service-less fragments were reported as missing a ceiling on a
  merge-blocking check, with a message telling the author to add something that
  lives in another file. They are skipped by name now, and an unreadable file is
  its own, separate failure.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / "docs/ssot/compose-resource-baseline.json"
COMPOSE_NAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)
# 512m, 1.5g, 2G, 1073741824. A bare 0, "0", "0b" or prose is not a ceiling.
SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgtb]?b?)\s*$", re.I)
UNITS = {
    "": 1,
    "b": 1,
    "k": 2**10,
    "kb": 2**10,
    "m": 2**20,
    "mb": 2**20,
    "g": 2**30,
    "gb": 2**30,
    "t": 2**40,
    "tb": 2**40,
}


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True
    ).stdout


def _tracked_composes() -> list[str]:
    return sorted(
        p for p in _git("ls-files").splitlines() if Path(p).name in COMPOSE_NAMES
    )


def _is_ceiling(value: object) -> bool:
    """A value that actually caps memory. Docker treats 0 as unlimited."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int | float):
        return value > 0
    if not isinstance(value, str):
        return False
    match = SIZE_RE.match(value)
    if not match:
        return False
    return float(match.group(1)) * UNITS.get(match.group(2).lower(), 0) > 0


def _unlimited_services(path: Path) -> tuple[list[str], str | None]:
    """(services with no ceiling, reason this file was skipped or unreadable)."""
    try:
        docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return [], f"unreadable: {type(exc).__name__}"
    bad: list[str] = []
    saw_services = False
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        # A fragment that only composes other files declares no service of its
        # own; the ceilings live where the services do.
        if "include" in doc and not doc.get("services"):
            return [], "include-only"
        services = doc.get("services")
        if not isinstance(services, dict):
            continue
        saw_services = True
        for name, spec in services.items():
            if not isinstance(spec, dict):
                bad.append(str(name))
                continue
            if "extends" in spec:
                continue  # the ceiling may be in the file it extends
            if _is_ceiling(spec.get("mem_limit")):
                continue
            node: object = spec.get("deploy")
            for key in ("resources", "limits"):
                node = node.get(key) if isinstance(node, dict) else None
            if isinstance(node, dict) and _is_ceiling(node.get("memory")):
                continue
            bad.append(str(name))
    if not saw_services:
        return [], "no services"
    return bad, None


def main() -> int:
    try:
        raw = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))["unlimited"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        print(f"cannot read {BASELINE_PATH.name}: refusing to judge", file=sys.stderr)
        return 1
    if not isinstance(raw, list) or any(not isinstance(e, str) for e in raw):
        print(
            f"{BASELINE_PATH.name}: 'unlimited' must be a list of strings",
            file=sys.stderr,
        )
        return 1
    if len(raw) != len(set(raw)):
        print(f"{BASELINE_PATH.name}: duplicate entries", file=sys.stderr)
        return 1
    baseline = set(raw)

    unlimited: set[str] = set()
    unreadable: list[str] = []
    for rel in _tracked_composes():
        bad, reason = _unlimited_services(ROOT / rel)
        if reason and reason.startswith("unreadable"):
            unreadable.append(f"{rel} ({reason})")
            continue
        unlimited.update(f"{rel}::{svc}" for svc in bad)

    if unreadable:
        print("compose files that could not be parsed:\n")
        for item in unreadable:
            print(f"  {item}")
        return 1

    new = sorted(unlimited - baseline)
    fixed = sorted(baseline - unlimited)
    if new:
        print("compose services without a memory ceiling, not in the baseline:\n")
        for item in new:
            print(f"  {item}")
        print(
            "\nops.standards.md §5: 每个容器必须声明资源限额。Give the service a\n"
            "`mem_limit:` (or `deploy.resources.limits.memory`) above its observed\n"
            "peak. A value of 0 is not a ceiling -- Docker reads it as unlimited."
        )
        return 1
    if fixed:
        print("baseline may only shrink — these now declare a ceiling, remove them:\n")
        for item in fixed:
            print(f"  {item}")
        return 1
    print(f"compose resource limits: {len(unlimited)} grandfathered, 0 new.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
