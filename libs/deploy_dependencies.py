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
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = _ROOT / "docs" / "ssot" / "deploy-dependencies.yaml"


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


def load_dependency_manifest(path: Path | str = DEFAULT_MANIFEST) -> dict[str, list[str]]:
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


def extra_dependency_globs(service_key: str, manifest: dict[str, list[str]] | None = None) -> list[str]:
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


def autodeploy_violations(
    composes, allowlist: set[str] | None = None
) -> list[str]:
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
