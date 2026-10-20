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

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.deploy.config_hash import (  # noqa: E402
    _compose_artifact_files,
    _compose_has_build,
)
from common.deploy.dependencies import (  # noqa: E402
    config_hash_input_count,
    fanout_coverage_violations,
    service_key_from_path,
)

# Canonical service roots whose layout service_key_from_path understands.
_CANONICAL_SERVICE_ROOTS = ("platform", "finance_report", "truealpha", "bootstrap")
_SERVICE_ROOTS = _CANONICAL_SERVICE_ROOTS
_REQUIRED_SERVICE_ROOTS = _CANONICAL_SERVICE_ROOTS

# Minimum (compose_count, input_count) per service root (#1123).
# Prevents silent drop when a service directory is renamed without a dot,
# `_SERVICE_ROOTS` is truncated, or bind mounts/contexts are rewritten to bare `${VAR}`.
_SERVICE_ROOT_FLOORS: dict[str, tuple[int, int]] = {
    "platform": (13, 350),
    "finance_report": (4, 8),
    "truealpha": (4, 9),
    "bootstrap": (4, 12),
}


def find_service_composes(root: Path = ROOT) -> list[Path]:
    """Every compose file in a service directory (including `*compose*.y*ml` templates)."""
    found: list[Path] = []
    for service_root in _SERVICE_ROOTS:
        base = root / service_root
        if not base.is_dir():
            continue
        for compose in sorted(base.rglob("*compose*.y*ml")):
            if compose.is_file() and service_key_from_path(
                compose.relative_to(root).as_posix()
            ):
                found.append(compose)
    return found


def find_unmapped_composes(root: Path = ROOT) -> list[str]:
    """Compose files under `_REQUIRED_SERVICE_ROOTS` that `service_key_from_path` maps to None."""
    unmapped: list[str] = []
    for service_root in _REQUIRED_SERVICE_ROOTS:
        base = root / service_root
        if not base.is_dir():
            continue
        for compose in sorted(base.rglob("*compose*.y*ml")):
            if not compose.is_file():
                continue
            rel = compose.relative_to(root).as_posix()
            if service_key_from_path(rel) is None:
                unmapped.append(rel)
    return unmapped


def service_root_counts(
    composes: list[Path], root: Path = ROOT
) -> dict[str, tuple[int, int]]:
    """Return `{service_root: (compose_count, input_count)}` across `composes`."""
    resolved_root = root.resolve()
    by_root: dict[str, list[Path]] = {sr: [] for sr in _REQUIRED_SERVICE_ROOTS}
    for compose in composes:
        try:
            rel = Path(compose).resolve().relative_to(resolved_root)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] in by_root:
            by_root[rel.parts[0]].append(Path(compose).resolve())
    return {
        sr: (
            len(paths),
            config_hash_input_count(paths, root=resolved_root) if paths else 0,
        )
        for sr, paths in by_root.items()
    }


def dockerfile_canary_violations(composes: list[Path], root: Path = ROOT) -> list[str]:
    """Ensure every deployed service with a `Dockerfile` or `build:` section reads at least one config-hash input (#1123)."""
    resolved_root = root.resolve()
    violations: list[str] = []
    by_dir: dict[Path, list[Path]] = {}
    for compose in composes:
        resolved_compose = Path(compose).resolve()
        by_dir.setdefault(resolved_compose.parent, []).append(resolved_compose)
    for service_dir, dir_composes in sorted(by_dir.items()):
        texts = [c.read_text(encoding="utf-8") for c in dir_composes]
        has_dockerfile_or_build = (service_dir / "Dockerfile").is_file() or any(
            _compose_has_build(text) for text in texts
        )
        if not has_dockerfile_or_build:
            continue
        total_inputs = sum(
            len(_compose_artifact_files(str(c), text))
            for c, text in zip(dir_composes, texts, strict=True)
        )
        if total_inputs < 1:
            rel_dir = service_dir.relative_to(resolved_root).as_posix()
            violations.append(
                f"{rel_dir}: has Dockerfile but config hash reads 0 inputs"
            )
    return violations


def floor_violations(
    composes: list[Path],
    root: Path = ROOT,
    *,
    counts: dict[str, tuple[int, int]] | None = None,
) -> list[str]:
    """Check unmapped composes, `_SERVICE_ROOTS` completeness, per-root floors, and Dockerfile canary."""
    violations: list[str] = []
    if tuple(_REQUIRED_SERVICE_ROOTS) != _CANONICAL_SERVICE_ROOTS:
        violations.append(
            f"_REQUIRED_SERVICE_ROOTS is missing canonical roots: {list(_REQUIRED_SERVICE_ROOTS)}"
        )
    missing_roots = [sr for sr in _CANONICAL_SERVICE_ROOTS if sr not in _SERVICE_ROOTS]
    if missing_roots:
        violations.append(
            f"_SERVICE_ROOTS is missing required service roots: {', '.join(missing_roots)}"
        )
    missing_floors = [
        sr for sr in _CANONICAL_SERVICE_ROOTS if sr not in _SERVICE_ROOT_FLOORS
    ]
    if missing_floors:
        violations.append(
            f"_SERVICE_ROOT_FLOORS is missing required service roots: {', '.join(missing_floors)}"
        )
    non_positive_floors = [
        sr
        for sr, (min_c, min_i) in _SERVICE_ROOT_FLOORS.items()
        if min_c < 1 or min_i < 1
    ]
    if non_positive_floors:
        violations.append(
            f"_SERVICE_ROOT_FLOORS has non-positive floors: {', '.join(sorted(non_positive_floors))}"
        )
    for rel in find_unmapped_composes(root=root):
        violations.append(
            f"{rel}: compose file does not map to a service key in service_key_from_path"
        )
    resolved_counts = (
        service_root_counts(composes, root=root) if counts is None else counts
    )
    for sr, (min_composes, min_inputs) in _SERVICE_ROOT_FLOORS.items():
        actual_composes, actual_inputs = resolved_counts.get(sr, (0, 0))
        if actual_composes < min_composes:
            violations.append(
                f"{sr}: found {actual_composes} compose files, below floor {min_composes}"
            )
        if actual_inputs < min_inputs:
            violations.append(
                f"{sr}: read {actual_inputs} config-hash inputs, below floor {min_inputs}"
            )
    violations.extend(dockerfile_canary_violations(composes, root=root))
    return violations


def _collect_violations(
    composes: list[Path],
    root: Path = ROOT,
    *,
    counts: dict[str, tuple[int, int]] | None = None,
) -> list[str]:
    """Single violation collection pipeline shared by `audit()` and `main()`."""
    try:
        coverage = (
            fanout_coverage_violations(composes)
            if root == ROOT
            else fanout_coverage_violations(composes, root=root)
        )
        structural = floor_violations(composes, root=root, counts=counts)
    except (FileNotFoundError, ValueError) as exc:
        return [str(exc)]
    return sorted(set(coverage) | set(structural))


def audit(root: Path = ROOT) -> list[str]:
    composes = (
        find_service_composes() if root == ROOT else find_service_composes(root=root)
    )
    return _collect_violations(composes, root=root)


def main(root: Path = ROOT) -> int:
    composes = (
        find_service_composes() if root == ROOT else find_service_composes(root=root)
    )
    if not composes:
        # A scan that finds no service reads no input. Reporting "passed" would hide it.
        print("ERROR: no service compose file found; the audit checked nothing.")
        return 1
    try:
        inputs = (
            config_hash_input_count(composes)
            if root == ROOT
            else config_hash_input_count(composes, root=root)
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: invalid compose or Dockerfile config-hash source: {exc}")
        return 1
    if inputs == 0:
        # Compose files that yield no config-hash input leave nothing to check.
        print("ERROR: the compose files yield no config-hash input; checked nothing.")
        return 1
    counts = service_root_counts(composes, root=root)
    all_violations = _collect_violations(composes, root=root, counts=counts)
    breakdown = ", ".join(
        f"{sr}: {c} compose files / {i} inputs" for sr, (c, i) in counts.items()
    )
    if not all_violations:
        print(
            "deploy fan-out coverage audit passed "
            f"({len(composes)} compose files, {inputs} inputs)"
        )
        for sr, (c, i) in counts.items():
            print(f"  {sr}: {c} compose files, {i} inputs")
        return 0

    print("ERROR: config-hash inputs that fan out to no service (under-fan-out risk):")
    for violation in all_violations:
        print(f"  - {violation}")
    print(f"Per-root summary: {breakdown}")
    print(
        "\nFix: add a glob that matches the file to the service's depends_on in "
        "docs/ssot/deploy-dependencies.yaml (e.g. `- common/**` or `- uv.lock`)."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
