"""Deploy dependency graph: which changed files fan out to which services.

Single source of truth for two things that must agree:
  1. The iac-runner change-detection fan-out (`sync_runner`), and
  2. The Deployer content config-hash (`libs/deploy/deployer`).

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

from libs.core.constants import REPO_ROOT

_ROOT = REPO_ROOT
DEFAULT_MANIFEST = _ROOT / "docs" / "ssot" / "deploy-dependencies.yaml"


def service_key_from_path(file_path: str) -> str | None:
    """Map a changed file under a service directory to its service key.

    Mirrors the layout used by the iac-runner SERVICE_TASK_MAP / ALL_SERVICES:
    platform/<NN>.<svc>/...        -> platform/<svc>
    finance_report/finance_report/<NN>.<svc>/... -> finance_report/<svc>
    truealpha/truealpha/<NN>.<svc>/... -> truealpha/<svc>
    bootstrap/<NN>.<svc>/...       -> bootstrap/<svc-with-dashes>
    Anything else (libs/, tools/, docs/, repo root) -> None (no own-dir owner).
    """
    parts = file_path.split("/")
    if parts[0] == "platform" and len(parts) >= 2 and "." in parts[1]:
        return f"platform/{parts[1].split('.', 1)[1]}"
    if parts[0] == "finance_report" and len(parts) >= 3 and "." in parts[2]:
        return f"finance_report/{parts[2].split('.', 1)[1]}"
    if parts[0] == "truealpha" and len(parts) >= 3 and "." in parts[2]:
        return f"truealpha/{parts[2].split('.', 1)[1]}"
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


def _config_hash_inputs(compose_paths, root: Path):
    """Yield (service key, input path) for every file the config hash reads (#1117).

    The config hash reads every file a service's compose builds from or mounts:
    the Dockerfile, its COPY/ADD sources resolved against the build context, and
    each relative bind mount (`config_hash._compose_artifact_files`).

    Raises on a compose file that is not valid YAML or not in a service
    directory: an unread compose has no inputs, and a guard that reads none
    passes while it checks nothing.
    """
    import yaml

    from libs.deploy.config_hash import _compose_artifact_files

    for compose in compose_paths:
        compose = Path(compose).resolve()
        key = service_key_from_path(compose.relative_to(root).as_posix())
        if key is None:
            raise ValueError(f"{compose} is not in a service directory")
        text = compose.read_text(encoding="utf-8")
        yaml.safe_load(text)
        for path in _compose_artifact_files(str(compose), text):
            yield key, path


def config_hash_input_count(compose_paths, root: Path = _ROOT) -> int:
    """How many files the config hash reads across `compose_paths`.

    Zero means the audit read nothing, so a pass would prove nothing.
    """
    return sum(1 for _ in _config_hash_inputs(compose_paths, root.resolve()))


def fanout_coverage_violations(
    compose_paths, manifest: dict[str, list[str]] | None = None, root: Path = _ROOT
) -> list[str]:
    """Config-hash inputs that would not fan out to their service (#1117).

    An input outside the service directory that no manifest glob matches changes
    the hash, yet a change to it selects no service, so a release leaves the
    service on stale input. Returns sorted "service_key: repo-relative path".
    """
    root = root.resolve()
    if manifest is None:
        manifest = load_dependency_manifest()
    violations: set[str] = set()
    for key, path in _config_hash_inputs(compose_paths, root):
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            violations.add(f"{key}: {path} (outside the repository)")
            continue
        if key not in match_changed_services([rel], manifest=manifest):
            violations.add(f"{key}: {rel}")
    return sorted(violations)
