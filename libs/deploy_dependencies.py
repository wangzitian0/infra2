"""Deploy dependency graph: which changed files fan out to which services.

Single source of truth for two things that must agree:
  1. The iac-runner change-detection fan-out (`sync_runner`), and
  2. The Deployer content config-hash (`libs/deployer`).

A service is deploy-dependent on its OWN directory (implicit) plus any EXTRA
build/config artifacts it declares in `docs/ssot/deploy-dependencies.yaml`.

Principle: fan-out follows BUILD/CONFIG dependencies (what a service bakes in),
NOT runtime connections. A shared Postgres config change must not redeploy its
consumers (they reconnect); only declare a dependency a service literally embeds.
Deploy tooling such as `libs/` and `tools/` is depended on by no service at
runtime (the runner re-checks-out new code), so it fans out to nothing — this
replaces the old `libs/ -> __all__` catch-all that redeployed everything.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = _ROOT / "docs" / "ssot" / "deploy-dependencies.yaml"

# Shared trees that fan out to NOTHING by default (deploy tooling / shared code).
# A service that COPYs one of these into its image at build time bakes the code
# in and MUST declare it in the manifest, or a change silently leaves it stale.
SHARED_TREES = ("libs", "tools", "common")


def service_key_from_path(file_path: str) -> str | None:
    """Map a changed file under a service directory to its service key.

    Mirrors the layout used by the iac-runner SERVICE_TASK_MAP / ALL_SERVICES:
    platform/<NN>.<svc>/...        -> platform/<svc>
    finance_report/finance_report/<NN>.<svc>/... -> finance_report/<svc>
    bootstrap/<NN>.<svc>/...       -> bootstrap/<svc-with-dashes>
    Anything else (libs/, tools/, docs/, repo root) -> None (no own-dir owner).
    """
    parts = file_path.split("/")
    if parts[0] == "platform" and len(parts) >= 2 and "." in parts[1]:
        return f"platform/{parts[1].split('.', 1)[1]}"
    if parts[0] == "finance_report" and len(parts) >= 3 and "." in parts[2]:
        return f"finance_report/{parts[2].split('.', 1)[1]}"
    if parts[0] == "bootstrap" and len(parts) >= 2 and "." in parts[1]:
        return f"bootstrap/{parts[1].split('.', 1)[1].replace('_', '-')}"
    return None


def load_dependency_manifest(
    path: Path | str = DEFAULT_MANIFEST,
) -> dict[str, list[str]]:
    """Return {service_key: [extra dependency globs]} from the manifest.

    The own-directory dependency is implicit and NOT listed here. Missing file or
    empty manifest -> no extra dependencies for anyone.
    """
    import yaml

    p = Path(path)
    if not p.exists():
        return {}
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    services = raw.get("services") or {}
    result: dict[str, list[str]] = {}
    for key, spec in services.items():
        deps = (spec or {}).get("depends_on") or []
        result[key] = [str(g) for g in deps]
    return result


def extra_dependency_globs(
    service_key: str, manifest: dict[str, list[str]] | None = None
) -> list[str]:
    """Declared extra dependency globs for one service (excludes its own dir)."""
    m = load_dependency_manifest() if manifest is None else manifest
    return list(m.get(service_key, []))


def match_changed_services(
    changed_files, manifest: dict[str, list[str]] | None = None
) -> set[str]:
    """Services affected by a set of changed files.

    Affected = a changed file is under the service's own directory OR matches one
    of its declared extra dependency globs. Tooling-only paths (libs/, tools/)
    match no service and therefore never fan out.
    """
    files = list(changed_files)
    if manifest is None:
        manifest = load_dependency_manifest()

    affected: set[str] = set()
    for file_path in files:
        key = service_key_from_path(file_path)
        if key:
            affected.add(key)
    for service_key, globs in manifest.items():
        if any(fnmatch.fnmatch(f, g) for g in globs for f in files):
            affected.add(service_key)
    return affected


def autodeploy_violations(composes, allowlist: set[str] | None = None) -> list[str]:
    """Names of composes with Dokploy `autoDeploy=true` that are not allowlisted.

    Necessity guard: IaC (the iac-runner) must be the single deploy trigger, so
    Dokploy's native autoDeploy must be off everywhere except an explicit
    allowlist of services intentionally left on Dokploy-native deploy.

    `composes`: iterable of dicts with at least `name` and `autoDeploy`.
    """
    allow = allowlist or set()
    return sorted(
        c.get("name", "<unknown>")
        for c in composes
        if c.get("autoDeploy") and c.get("name") not in allow
    )


# --- Observability -----------------------------------------------------------


@dataclass(frozen=True)
class FanoutDecision:
    """Explained fan-out: which services were selected and why, plus drops.

    `selected` maps each affected service to a human-readable reason. `dropped`
    lists changed files that fanned out to NOTHING (tooling/shared paths no
    service bakes in) — the signal that distinguishes "correctly skipped" from
    "silently under-deployed" when debugging a no-op run.
    """

    selected: dict[str, str]
    dropped: list[str] = field(default_factory=list)


def explain_fanout(
    changed_files, manifest: dict[str, list[str]] | None = None
) -> FanoutDecision:
    """Like match_changed_services, but records WHY each service was selected.

    Reasons are stable strings: "own-dir (<file>)" or "declared dep (<file>)".
    Own-dir selection wins over a declared-dep reason for the same service.
    """
    files = list(changed_files)
    if manifest is None:
        manifest = load_dependency_manifest()

    selected: dict[str, str] = {}
    matched: set[str] = set()
    for file_path in files:
        key = service_key_from_path(file_path)
        if key:
            selected.setdefault(key, f"own-dir ({file_path})")
            matched.add(file_path)
    for service_key, globs in manifest.items():
        hits = [f for f in files if any(fnmatch.fnmatch(f, g) for g in globs)]
        if hits:
            selected.setdefault(service_key, f"declared dep ({hits[0]})")
            matched.update(hits)

    dropped = [f for f in files if f not in matched]
    return FanoutDecision(selected=selected, dropped=dropped)


def dockerfile_baked_shared_trees(dockerfile_text: str) -> set[str]:
    """Top-level SHARED_TREES a Dockerfile COPY/ADDs from the build context.

    Detects `COPY libs /app/libs`, `ADD tools/x .`, etc. Multi-stage copies
    (`COPY --from=...`) are ignored: they pull from a prior image layer, not the
    repo build context, so they create no source-tree deploy dependency.
    """
    trees: set[str] = set()
    for raw in dockerfile_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        instr = parts[0].upper()
        if instr not in {"COPY", "ADD"}:
            continue
        operands = parts[1:]
        flags = [p for p in operands if p.startswith("--")]
        if any(f.startswith("--from") for f in flags):
            continue  # copies from another build stage, not the context
        positional = [p for p in operands if not p.startswith("--")]
        sources = positional[:-1] if len(positional) >= 2 else positional
        for src in sources:
            top = src.lstrip("./").split("/")[0]
            if top in SHARED_TREES:
                trees.add(top)
    return trees


def fanout_coverage_violations(
    service_dockerfiles: dict[str, str], manifest: dict[str, list[str]] | None = None
) -> list[str]:
    """Services that bake a shared tree into their image but don't declare it.

    A violation is an under-fan-out landmine: the Dockerfile COPYs a shared tree
    (libs/tools/common) so the service runs that code, yet no manifest glob would
    fan a change in that tree out to it — a change would silently leave the
    service running stale baked-in code. Returns sorted "service_key: tree".
    """
    if manifest is None:
        manifest = load_dependency_manifest()
    violations: list[str] = []
    for service_key, text in service_dockerfiles.items():
        globs = manifest.get(service_key, [])
        for tree in dockerfile_baked_shared_trees(text):
            probe = f"{tree}/__changed_probe__"
            if not any(fnmatch.fnmatch(probe, g) for g in globs):
                violations.append(f"{service_key}: {tree}")
    return sorted(violations)
