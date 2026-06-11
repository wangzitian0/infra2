#!/usr/bin/env python3
"""Guard the deploy dependency graph against silent under-fan-out.

Static audit (no live API): scans every service Dockerfile for shared trees
(libs/tools/common) it COPYs into its image and verifies each is declared in
docs/ssot/deploy-dependencies.yaml. An undeclared baked-in tree is an
under-fan-out landmine — a change to it would not redeploy the service, leaving
it running stale baked-in code (the class of bug behind #267's alerting gap).

Exits non-zero on violation, so it serves as both a PR gate (infra-ci) and a
scheduled monitor (deploy-guard-audit workflow alerts on the non-zero exit).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.deploy_dependencies import (  # noqa: E402
    fanout_coverage_violations,
    service_key_from_path,
)

# Service roots whose layout service_key_from_path understands.
_SERVICE_ROOTS = ("platform", "finance_report", "bootstrap")


def find_service_dockerfiles(root: Path = ROOT) -> dict[str, str]:
    """Map service_key -> Dockerfile text for every service-owned Dockerfile."""
    found: dict[str, str] = {}
    for service_root in _SERVICE_ROOTS:
        base = root / service_root
        if not base.is_dir():
            continue
        for dockerfile in sorted(base.rglob("Dockerfile*")):
            if not dockerfile.is_file():
                continue
            key = service_key_from_path(dockerfile.relative_to(root).as_posix())
            if not key:
                continue
            text = dockerfile.read_text(encoding="utf-8", errors="replace")
            # Concatenate when a service ships more than one Dockerfile so every
            # baked-in tree is considered.
            found[key] = f"{found[key]}\n{text}" if key in found else text
    return found


def audit() -> list[str]:
    return fanout_coverage_violations(find_service_dockerfiles())


def main() -> int:
    violations = audit()
    if not violations:
        print("deploy fan-out coverage audit passed")
        return 0

    print("ERROR: undeclared baked-in shared trees (under-fan-out risk):")
    for violation in violations:
        print(f"  - {violation}")
    print(
        "\nFix: add the tree to the service's depends_on in "
        "docs/ssot/deploy-dependencies.yaml (e.g. `- libs/**`)."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
