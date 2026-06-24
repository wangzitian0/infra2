"""Single service registry derived from the IaC Deployer classes.

The `deploy.py` Deployer subclass of each service is the SINGLE source of truth
for that service's facts (subdomain, ports, prod-only, etc.). Historically those
facts were re-copied by hand into many parallel lists — INFRA_PROBE_SPECS,
watchdog-signals.yaml, wrangler targets, common.py dicts, sync_runner's
ALL_SERVICES — which drift silently (see Infra-013).

This module reads the Deployer attributes ONCE (via AST, no import side effects)
and exposes `get_*`-style accessors so every downstream config can be DERIVED
from, or audited against, the registry instead of hand-maintained.

Scope of the scan mirrors `libs.deployer.discover_services` (the `platform` and
`finance_report` layers) so `all_services()` is an exact superset-free match of
the deploy fan-out list.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# (layer name, layer path) — mirrors libs.deployer.discover_services.
_LAYERS: dict[str, Path] = {
    "platform": REPO_ROOT / "platform",
    "finance_report": REPO_ROOT / "finance_report" / "finance_report",
}

PRODUCTION = "production"


@dataclass(frozen=True)
class ServiceMeta:
    """Facts a service's deploy.py Deployer declares. None = attribute unset."""

    service_id: str  # e.g. "platform/signoz"
    layer: str  # "platform" | "finance_report"
    service: str  # the `service` attribute, e.g. "signoz"
    prod_only: bool  # True -> not deployed to non-production envs
    subdomain: str | None  # public subdomain, e.g. "sso"; None = no public route
    service_port: int | None  # container port for routing
    service_name: str | None  # compose service name (multi-service composes)
    project: str  # Dokploy project, defaults to "platform"


def service_attrs() -> dict[str, ServiceMeta]:
    """Map service_id -> ServiceMeta for every service with a deploy.py."""
    result: dict[str, ServiceMeta] = {}
    for layer, layer_path in _LAYERS.items():
        if not layer_path.exists():
            continue
        for service_dir in sorted(layer_path.iterdir()):
            if not service_dir.is_dir():
                continue
            parts = service_dir.name.split(".", 1)
            if len(parts) != 2:
                continue
            deploy_file = service_dir / "deploy.py"
            if not deploy_file.exists():
                continue
            service_name_dir = parts[1]
            tree = ast.parse(deploy_file.read_text(encoding="utf-8"))
            result[f"{layer}/{service_name_dir}"] = ServiceMeta(
                service_id=f"{layer}/{service_name_dir}",
                layer=layer,
                service=_class_attr(tree, "service") or service_name_dir,
                prod_only=bool(_class_attr(tree, "prod_only") or False),
                subdomain=_class_attr(tree, "subdomain"),
                service_port=_class_attr(tree, "service_port"),
                service_name=_class_attr(tree, "service_name"),
                project=_class_attr(tree, "project") or "platform",
            )
    return result


def all_services() -> list[str]:
    """Every service_id, sorted. Equals libs.deployer.discover_services keys."""
    return sorted(service_attrs())


def services_in_env(env: str) -> list[str]:
    """Service_ids that actually deploy to `env` (prod_only excluded off-prod)."""
    is_prod = env == PRODUCTION
    return sorted(
        meta.service_id
        for meta in service_attrs().values()
        if is_prod or not meta.prod_only
    )


def shared_services() -> set[str]:
    """Service_ids that run as a single shared (prod) instance for all envs."""
    return {m.service_id for m in service_attrs().values() if m.prod_only}


def subdomains() -> dict[str, str]:
    """Service_id -> public subdomain, only for services that declare one."""
    return {
        m.service_id: m.subdomain
        for m in service_attrs().values()
        if m.subdomain
    }


def probe_container_bases() -> dict[str, ServiceMeta]:
    """Exact compose container base name (``{layer}-{service|service_name}``, BEFORE any
    ``${ENV_SUFFIX}``) -> ServiceMeta. Both ``service`` and ``service_name`` map. This is the
    EXACT-match index; to resolve a real probe/route/DNS host (which may be a longer
    sub-container name) use :func:`resolve_container_host`, not a bare ``.get`` on this dict.
    """
    bases: dict[str, ServiceMeta] = {}
    for meta in service_attrs().values():
        for name in (meta.service, meta.service_name):
            if name:
                bases[f"{meta.layer}-{name}"] = meta
    return bases


def resolve_container_host(host: str) -> ServiceMeta | None:
    """Resolve a compose container hostname (with or without ``${ENV_SUFFIX}``) to its
    ServiceMeta, so a probe / route / DNS target can be bound back to the registry.

    Tries an exact base match first, then the longest registry base that is a ``-``-delimited
    prefix — so multi-container sub-services (``platform-signoz-otel-collector``,
    ``platform-openpanel-api``, ``platform-authentik-server``) resolve to their PARENT service's
    deploy facts, which they share (same compose, same env, same ``prod_only``). The ``-``
    boundary prevents ``platform-redis`` from spuriously matching ``platform-redisX``. Returns
    None for hosts outside the platform/finance_report registry scan (e.g. bootstrap dokploy /
    vault) so callers can knowingly skip them rather than mis-resolve.
    """
    base = host.replace("${ENV_SUFFIX}", "")
    bases = probe_container_bases()
    if base in bases:
        return bases[base]
    candidates = [name for name in bases if base.startswith(f"{name}-")]
    return bases[max(candidates, key=len)] if candidates else None


def _class_attr(tree: ast.Module, attr_name: str) -> str | int | bool | None:
    """Return the literal value of the Deployer subclass's class attribute.

    Only TOP-LEVEL classes that subclass `Deployer` are considered, so a helper
    or nested class that happens to declare the same attribute name cannot shadow
    the registry (matches the docstring's "the Deployer class" guarantee).

    Handles str / int / bool constants. `x = None` and non-literal values
    (calls, names) read back as None — the attribute is treated as unset.
    """
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or not _is_deployer_class(node):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign):
                continue
            if not any(
                isinstance(t, ast.Name) and t.id == attr_name for t in stmt.targets
            ):
                continue
            if isinstance(stmt.value, ast.Constant) and isinstance(
                stmt.value.value, (str, int, bool)
            ):
                return stmt.value.value
    return None


def _is_deployer_class(node: ast.ClassDef) -> bool:
    """True if the class subclasses a `*Deployer` base (e.g. `class X(Deployer)`)."""
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id.endswith("Deployer"):
            return True
        if isinstance(base, ast.Attribute) and base.attr.endswith("Deployer"):
            return True
    return False
