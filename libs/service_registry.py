"""Single service registry derived from the IaC Deployer classes.

The `deploy.py` Deployer subclass of each service is the SINGLE source of truth
for that service's facts (subdomain, ports, prod-only, etc.). Historically those
facts were re-copied by hand into many parallel lists — INFRA_PROBE_SPECS,
watchdog-signals.yaml, wrangler targets, common.py dicts, sync_runner's
ALL_SERVICES — which drift silently (see Infra-013).

This module reads the Deployer attributes ONCE (via AST, no import side effects)
and exposes `get_*`-style accessors so every downstream config can be DERIVED
from, or audited against, the registry instead of hand-maintained.

`_LAYERS` below is the single source of truth for the layer name -> path mapping.
`libs.deploy.deployer.discover_services` imports it directly (no second copy) so
`all_services()` is an exact superset-free match of the deploy fan-out list.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from libs.service_facets import (
    FACET_CLASSES,
    BackupFacet,
    Exemption,
    ProbeFacet,
    SecretsFacet,
    SignalFacet,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# (layer name, layer path) — the single source of truth; libs.deploy.deployer
# and tools/loader.py both derive from this instead of hand-copying it.
_LAYERS: dict[str, Path] = {
    "platform": REPO_ROOT / "platform",
    "finance_report": REPO_ROOT / "finance_report" / "finance_report",
    "truealpha": REPO_ROOT / "truealpha" / "truealpha",
}

PRODUCTION = "production"

_EXTERNAL_COMPONENT_IDS = {
    "1password-connect": "bootstrap/1password",
    "cloudflare-watchdog": "infra/cloudflare-watchdog",
    "docker": "infra/docker",
    "dokploy": "bootstrap/dokploy",
    "finance-report-api": "finance_report/app",
    "finance-report-web": "finance_report/app",
    "host": "infra/host",
    # the IaC control plane itself (facet reconcile, #542) — a pseudo-id like
    # infra/host: monitored, but not a deployable service
    "iac": "infra/iac",
    "iac-runner": "bootstrap/iac-runner",
    "vault": "bootstrap/vault",
}

_BOOTSTRAP_COMPOSE_IDS = {
    "1password-connect": "bootstrap/1password",
    "iac_runner": "bootstrap/iac-runner",
    "vault": "bootstrap/vault",
}


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
    telemetry_service_name: str | None  # OTEL service.name override
    telemetry_component: str | None  # OTEL infra.component override
    project: str  # Dokploy project, defaults to "platform"
    # Deploy artifact facts (#542): the compose file this Deployer ships and the
    # persistent data root it owns (None = stateless). Literal string attrs on
    # the Deployer, read for facet derivations (vault inventory, backup
    # inventory) so those expectations track the SAME deploy-side declaration.
    compose_path: str | None = None
    data_path: str | None = None
    # Rollout state (#542): True = this service has no production deployment yet
    # (deliberately staging-scoped, e.g. truealpha's #500 rollout). Consumed by
    # tools/reconcile_iac_inputs.py's prod selection and the vault self-refresh
    # audit's production exclusion — remove the Deployer attr when the service
    # is actually promoted, and both derivations follow.
    not_yet_in_production: bool = False
    # Facet declarations (#541): typed per-service operational facts, read from
    # the same Deployer class via AST. See libs/service_facets.py.
    probes: tuple[ProbeFacet, ...] = ()
    signals: tuple[SignalFacet, ...] = ()
    backups: tuple[BackupFacet, ...] = ()
    secrets: tuple[SecretsFacet, ...] = ()
    exemptions: tuple[Exemption, ...] = ()
    deploy_v2_canary: bool = False
    # Dedicated product domain override (e.g. truealpha/app -> truealpha.club);
    # None = use whatever shared INTERNAL_DOMAIN the deploy request/caller passes
    # in (every service until truealpha/app). Kept last, with a default, so
    # existing ServiceMeta(**kwargs) call sites that predate this field
    # (tests, fixtures) keep constructing without knowing about it.
    domain: str | None = None

    def exempted(self, check_id: str) -> bool:
        """True if this service explicitly opted out of facet ``check_id``."""
        return any(e.check_id == check_id for e in self.exemptions)


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
            result[f"{layer}/{service_name_dir}"] = _meta_from_deploy_file(
                deploy_file, service_id=f"{layer}/{service_name_dir}", layer=layer
            )
    return result


def _meta_from_deploy_file(
    deploy_file: Path, *, service_id: str, layer: str
) -> ServiceMeta:
    """Build one ServiceMeta from a deploy.py via AST (never imported)."""
    service_name_dir = service_id.split("/", 1)[1]
    tree = ast.parse(deploy_file.read_text(encoding="utf-8"))
    where = str(deploy_file.relative_to(REPO_ROOT))
    return ServiceMeta(
        service_id=service_id,
        layer=layer,
        service=_class_attr(tree, "service") or service_name_dir,
        prod_only=bool(_class_attr(tree, "prod_only") or False),
        subdomain=_class_attr(tree, "subdomain"),
        domain=_class_attr(tree, "domain"),
        service_port=_class_attr(tree, "service_port"),
        service_name=_class_attr(tree, "service_name"),
        telemetry_service_name=_class_attr(tree, "telemetry_service_name"),
        telemetry_component=_class_attr(tree, "telemetry_component"),
        project=_class_attr(tree, "project") or "platform",
        compose_path=_class_attr(tree, "compose_path"),
        data_path=_class_attr(tree, "data_path"),
        not_yet_in_production=bool(
            _class_attr(tree, "not_yet_in_production") or False
        ),
        probes=_facet_seq(tree, "probes", ProbeFacet, where),
        signals=_facet_seq(tree, "signals", SignalFacet, where),
        backups=_facet_seq(tree, "backups", BackupFacet, where),
        secrets=_facet_seq(tree, "secrets", SecretsFacet, where),
        exemptions=_facet_seq(tree, "exemptions", Exemption, where),
        deploy_v2_canary=bool(_class_attr(tree, "deploy_v2_canary") or False),
    )


# Facet-only bootstrap-plane deploy.py files (#542). The bootstrap plane is
# deployed by bootstrap tooling and is deliberately OUTSIDE the registry scan —
# `_LAYERS` drives the deploy fan-out (`all_services()` / discover_services),
# and bootstrap must never enter that fan-out. But bootstrap/iac_runner's
# deploy.py is still the single declaration point for its operational facets
# (SecretsFacet, backups), so facet derivations read it through the SAME
# fail-closed AST reader without registering it as a deployable service.
_BOOTSTRAP_FACET_FILES: dict[str, Path] = {
    "bootstrap/iac_runner": REPO_ROOT / "bootstrap/06.iac_runner/deploy.py",
}


def bootstrap_facet_attrs() -> dict[str, ServiceMeta]:
    """Facet-only ServiceMetas for the bootstrap plane (NOT in service_attrs()).

    Keys never overlap `all_services()`; consumers that need the bootstrap
    plane's facets (e.g. the vault self-refresh inventory derivation) merge
    this in explicitly.
    """
    return {
        service_id: _meta_from_deploy_file(
            deploy_file, service_id=service_id, layer="bootstrap"
        )
        for service_id, deploy_file in _BOOTSTRAP_FACET_FILES.items()
    }


def all_services() -> list[str]:
    """Every service_id, sorted. Equals libs.deploy.deployer.discover_services keys."""
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
    return {m.service_id: m.subdomain for m in service_attrs().values() if m.subdomain}


def domain_for_service(service_id: str) -> str | None:
    """The service's dedicated domain override, or None if it uses the shared
    INTERNAL_DOMAIN a caller passes in (every service until truealpha/app)."""
    meta = service_attrs().get(service_id)
    return meta.domain if meta else None


def service_identity(
    service_id: str,
    environment: str,
    *,
    component: str | None = None,
    version: str = "",
    iac_ref: str = "",
):
    """Build the canonical cross-plane identity from registry-owned facts."""
    from libs.service_identity import ServiceIdentity

    meta = service_attrs().get(service_id)
    if meta is None:
        raise ValueError(f"unknown registered service_id: {service_id}")
    return ServiceIdentity.build(
        service_id,
        environment,
        component=component or meta.service,
        service_name=meta.telemetry_service_name or meta.service,
        version=version,
        iac_ref=iac_ref,
    )


def service_id_for_component(component: str, *, signal: str = "") -> str:
    """Resolve monitoring component aliases to one canonical service ID."""
    normalized = component.strip().lower().replace("_", "-")
    if normalized in _EXTERNAL_COMPONENT_IDS:
        return _EXTERNAL_COMPONENT_IDS[normalized]

    attrs = service_attrs()
    candidates = [
        meta.service_id
        for meta in attrs.values()
        if meta.service.replace("_", "-") == normalized
    ]
    if len(candidates) == 1:
        return candidates[0]

    signal_prefix = signal.strip().lower().split("-", 1)[0]
    scoped = [item for item in candidates if item.split("/", 1)[0] == signal_prefix]
    if len(scoped) == 1:
        return scoped[0]
    raise ValueError(
        f"component {component!r} does not resolve to one service_id "
        f"(signal={signal!r}, candidates={sorted(candidates)})"
    )


def service_id_for_dokploy(project: str, compose_name: str) -> str | None:
    """Resolve a Dokploy project/compose coordinate without guessing globally.

    Compose names such as ``app`` and ``postgres`` are intentionally resolved
    inside their project namespace. Unknown manually-created composes return
    ``None`` so monitoring can surface them as unregistered rather than assign
    a false identity.
    """
    canonical_project = (project or "").strip().lower().replace("-", "_")
    canonical_compose = (compose_name or "").strip().lower()
    if canonical_project == "bootstrap":
        return _BOOTSTRAP_COMPOSE_IDS.get(canonical_compose)

    matches = [
        meta.service_id
        for meta in service_attrs().values()
        if meta.project == canonical_project and meta.service == canonical_compose
    ]
    return matches[0] if len(matches) == 1 else None


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
    base = base.replace("finance-report-", "finance_report-")
    for suffix in ("-staging", "-preview"):
        base = base.replace(suffix, "")
    bases = probe_container_bases()
    if base in bases:
        return bases[base]
    candidates = [name for name in bases if base.startswith(f"{name}-")]
    return bases[max(candidates, key=len)] if candidates else None


def _deployer_assign(tree: ast.Module, attr_name: str) -> ast.expr | None:
    """The value node assigned to ``attr_name`` on the top-level Deployer subclass.

    Only TOP-LEVEL classes that subclass `Deployer` are considered, so a helper
    or nested class that happens to declare the same attribute name cannot shadow
    the registry (matches the docstring's "the Deployer class" guarantee).
    """
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or not _is_deployer_class(node):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign):
                continue
            if any(isinstance(t, ast.Name) and t.id == attr_name for t in stmt.targets):
                return stmt.value
    return None


def _class_attr(tree: ast.Module, attr_name: str) -> str | int | bool | None:
    """Return the literal value of the Deployer subclass's class attribute.

    Handles str / int / bool constants. `x = None` and non-literal values
    (calls, names) read back as None — the attribute is treated as unset.
    """
    value = _deployer_assign(tree, attr_name)
    if (
        value is not None
        and isinstance(value, ast.Constant)
        and isinstance(value.value, (str, int, bool))
    ):
        return value.value
    return None


def _facet_instance(node: ast.expr, facet_cls: type, where: str):
    """Build a facet instance from a LITERAL constructor Call node.

    FAIL CLOSED: anything other than ``FacetCls(<literal args>)`` raises — a
    facet the reader cannot evaluate must break CI loudly, never silently drop
    (e.g. a probe vanishing from the rendered INFRA_PROBE_SPECS)."""
    if not isinstance(node, ast.Call):
        raise ValueError(
            f"{where}: facet entries must be literal {facet_cls.__name__}(...) "
            f"constructor calls (AST-read, never imported); got {ast.dump(node)[:80]}"
        )
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
    if FACET_CLASSES.get(name) is not facet_cls:
        raise ValueError(
            f"{where}: expected a {facet_cls.__name__}(...) call, got {name!r}"
        )
    try:
        args = [ast.literal_eval(arg) for arg in node.args]
        kwargs = {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords if kw.arg}
        return facet_cls(*args, **kwargs)
    except (ValueError, TypeError, SyntaxError) as exc:
        raise ValueError(
            f"{where}: {facet_cls.__name__} arguments must be literals "
            f"(no names/f-strings/expressions): {exc}"
        ) from exc


def _facet_seq(tree: ast.Module, attr_name: str, facet_cls: type, where: str) -> tuple:
    """The Deployer's ``attr_name`` facet tuple, AST-evaluated. Absent -> ()."""
    value = _deployer_assign(tree, attr_name)
    if value is None:
        return ()
    if not isinstance(value, (ast.Tuple, ast.List)):
        raise ValueError(
            f"{where}: `{attr_name}` must be a literal tuple/list of "
            f"{facet_cls.__name__}(...) calls"
        )
    return tuple(_facet_instance(el, facet_cls, where) for el in value.elts)


def _facet_one(tree: ast.Module, attr_name: str, facet_cls: type, where: str):
    """The Deployer's single-facet ``attr_name``, AST-evaluated. Absent/None -> None."""
    value = _deployer_assign(tree, attr_name)
    if value is None:
        return None
    if isinstance(value, ast.Constant) and value.value is None:
        return None
    return _facet_instance(value, facet_cls, where)


def _is_deployer_class(node: ast.ClassDef) -> bool:
    """True if the class subclasses a `*Deployer` base (e.g. `class X(Deployer)`)."""
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id.endswith("Deployer"):
            return True
        if isinstance(base, ast.Attribute) and base.attr.endswith("Deployer"):
            return True
    return False
