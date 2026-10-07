#!/usr/bin/env python3
"""Guard the deploy dependency graph against silent under-fan-out.

Static audit (no live API): for every service compose file, enumerates the
files its config hash reads (Dockerfile, COPY/ADD sources against the build
context, bind mounts) and verifies that each one outside the service directory
fans out to the service through docs/ssot/deploy-dependencies.yaml. An input
that fans out to nothing is an under-fan-out landmine: a change to it would not
redeploy the service, leaving it on stale input (#267's alerting gap, #1117).

Exits non-zero on violation, so it serves as both a PR gate (infra-ci) and a
scheduled monitor (ops-checks deploy-guard-audit task alerts on the non-zero exit).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.deploy.dependencies import (  # noqa: E402
    fanout_coverage_violations,
    service_key_from_path,
)

# Service roots whose layout service_key_from_path understands.
_SERVICE_ROOTS = ("platform", "finance_report", "truealpha", "bootstrap")


def find_service_composes(root: Path = ROOT) -> list[Path]:
    """Every compose file in a service directory."""
    found: list[Path] = []
    for service_root in _SERVICE_ROOTS:
        base = root / service_root
        if not base.is_dir():
            continue
        for compose in sorted(base.rglob("compose*.y*ml")):
            if compose.is_file() and service_key_from_path(
                compose.relative_to(root).as_posix()
            ):
                found.append(compose)
    return found


def audit() -> list[str]:
    return fanout_coverage_violations(find_service_composes())


def main() -> int:
    composes = find_service_composes()
    if not composes:
        # A scan that finds no service reads no input. Reporting "passed" would hide it.
        print("ERROR: no service compose file found; the audit checked nothing.")
        return 1
    violations = fanout_coverage_violations(composes)
    if not violations:
        print(f"deploy fan-out coverage audit passed ({len(composes)} compose files)")
        return 0

    print("ERROR: config-hash inputs that fan out to no service (under-fan-out risk):")
    for violation in violations:
        print(f"  - {violation}")
    print(
        "\nFix: add a glob that matches the file to the service's depends_on in "
        "docs/ssot/deploy-dependencies.yaml (e.g. `- libs/**` or `- uv.lock`)."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
