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
import subprocess
import sys
from pathlib import Path

import yaml

from infra2_sdk.rules.compose import inspect_compose

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / "docs/ssot/compose-resource-baseline.json"
COMPOSE_NAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True
    ).stdout


def _tracked_composes() -> list[str]:
    return sorted(
        p for p in _git("ls-files").splitlines() if Path(p).name in COMPOSE_NAMES
    )


def _unlimited_services(path: Path) -> tuple[list[str], str | None]:
    """(services with no ceiling, reason this file was skipped or unreadable)."""
    report = inspect_compose(path)
    if report.errors:
        return [], f"unreadable: {report.errors[0]}"
    try:
        docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]
        if any(
            isinstance(d, dict) and "include" in d and not d.get("services")
            for d in docs
        ):
            return [], "include-only"
        if not any(
            isinstance(d, dict) and isinstance(d.get("services"), dict) for d in docs
        ):
            return [], "no services"
    except Exception as exc:
        return [], f"unreadable: {type(exc).__name__}"
    return list(report.unlimited_services), None


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
    bare_latest: list[tuple[str, str]] = []
    for rel in _tracked_composes():
        report = inspect_compose(ROOT / rel)
        for ref in report.bare_latest_violations:
            bare_latest.append((rel, ref))
        if report.errors:
            unreadable.append(f"{rel} ({'; '.join(report.errors)})")
            continue
        unlimited.update(f"{rel}::{svc}" for svc in report.unlimited_services)

    if unreadable:
        print("compose files that could not be parsed:\n")
        for item in unreadable:
            print(f"  {item}")
        return 1

    if bare_latest:
        print("❌ Bare ':latest' image tags are not allowed (pin a digest):")
        for rel, ref in bare_latest:
            print(f"   {rel}: image: {ref}")
        print(
            "\nPin with `image: <repo>:<tag>@sha256:<digest>` "
            "(get it via `docker inspect <image> --format '{{index .RepoDigests 0}}'`)."
        )
        return 1

    new = sorted(unlimited - baseline)
    # An entry whose file no longer exists is stale, not "now limited" -- the
    # two need different messages or a rename reads as a fix.
    gone = sorted(
        e for e in baseline - unlimited if not (ROOT / e.split("::", 1)[0]).exists()
    )
    fixed = sorted(e for e in baseline - unlimited if e not in gone)
    if new:
        print("compose services without a memory ceiling, not in the baseline:\n")
        for item in new:
            print(f"  {item}")
        print(
            "\nops.standards.md §5: 每个容器必须声明资源限额。Give the service a\n"
            "`mem_limit:` above its observed peak -- the SSOT names compose fields\n"
            "(mem_limit / mem_reservation / cpu_shares), not deploy.resources.\n"
            "A value of 0 is not a ceiling: Docker reads it as unlimited."
        )
        return 1
    if fixed or gone:
        print("baseline may only shrink — update it:\n")
        for item in fixed:
            print(f"  now declares a ceiling, remove: {item}")
        for item in gone:
            print(f"  file no longer exists, remove: {item}")
        return 1
    print(f"compose resource limits: {len(unlimited)} grandfathered, 0 new.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
