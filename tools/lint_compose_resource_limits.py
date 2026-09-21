#!/usr/bin/env python3
"""Every compose service must declare a memory ceiling; the exceptions may only shrink.

`docs/ssot/ops.standards.md` §5 makes this a 必须, and it was written from a
measured incident: "2026-06 prefect 占 3G、零限额下内存 27/31G、Dokploy 控制面
超时". Its blacklist adds "禁止无限额服务上 prod". Nothing checked it. An audit
found 13 committed compose files with no ceiling at all, including
platform/01.postgres, platform/10.authentik, platform/11.signoz,
bootstrap/05.vault and bootstrap/06.iac_runner -- and prod, staging, preview and
playground share one VPS, so a single leak takes the control plane with it,
including Dokploy, which is the means of recovery.

Ratchet, not big bang. Those 13 are recorded in BASELINE and allowed to stay;
anything not on that list must declare a ceiling, and a file that gains one is
removed from the baseline by the same lint that would otherwise let it drift
back. The list may only get shorter, which is the property a big-bang fix
cannot give: it makes the debt visible and stops it growing while it is paid.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / "docs/ssot/compose-resource-baseline.json"
# A ceiling can be spelled either way; both are what the SSOT's table means.
CEILING_KEYS = ("mem_limit", "deploy")


def _tracked_composes() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "*/compose.yaml", "*/compose.yml"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(p for p in out.splitlines() if p)


def _declares_ceiling(path: Path) -> bool:
    """True when every service in the file declares a memory ceiling.

    A file with one limited service and one unlimited service is not compliant:
    the unlimited one is the one that eats the host.
    """
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        # Unparseable means we cannot say it is compliant. `validate-compose`
        # already fails on a malformed file, so this never silently passes.
        return False
    services = (doc or {}).get("services")
    if not isinstance(services, dict) or not services:
        return False
    for spec in services.values():
        if not isinstance(spec, dict):
            return False
        if "mem_limit" in spec:
            continue
        limits = ((spec.get("deploy") or {}).get("resources") or {}).get("limits") or {}
        if "memory" not in limits:
            return False
    return True


def main() -> int:
    try:
        baseline = set(
            json.loads(BASELINE_PATH.read_text(encoding="utf-8"))["unlimited"]
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        print(
            f"cannot read {BASELINE_PATH.relative_to(ROOT)}: refusing to judge",
            file=sys.stderr,
        )
        return 1

    unlimited = {p for p in _tracked_composes() if not _declares_ceiling(ROOT / p)}
    new = sorted(unlimited - baseline)
    fixed = sorted(baseline - unlimited)
    stale = sorted(b for b in baseline if not (ROOT / b).exists())

    if new:
        print("compose services without a memory ceiling, not in the baseline:\n")
        for path in new:
            print(f"  {path}")
        print(
            "\nops.standards.md §5: 每个容器必须声明资源限额。Add `mem_limit:` (or\n"
            "`deploy.resources.limits.memory`) to every service in the file. The\n"
            "ceiling belongs above the observed peak; prod's total is capped at 50%\n"
            "of the host."
        )
        return 1
    if fixed or stale:
        print("baseline is out of date — it may only shrink, so update it:\n")
        for path in fixed:
            print(f"  now limited, remove from baseline: {path}")
        for path in stale:
            print(f"  no longer exists, remove from baseline: {path}")
        return 1

    print(f"compose resource limits: {len(unlimited)} grandfathered, 0 new.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
