"""Bind the Cloudflare worker's public-route targets to the service registry.

The internal-probe analog of this lives in test_service_registry_downstream.py (#430). This is
the PUBLIC-route half: the worker.js TARGETS list hand-copies one row per public route per env,
and the staging/prod split must agree with the registry's `prod_only` flag. The motivating bug
(infra2#307) was exactly a staging public-route to a single-instance (prod_only) service:
`sso-staging`-style host that never exists -> a permanent false-positive alert.
"""

from __future__ import annotations

import re
from pathlib import Path

from libs import service_registry

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "cloudflare/infra-watchdog/worker.js"
# ["production", "minio-public-route", "platform/minio", "https://...", ...],
_ROW_RE = re.compile(
    r'\[\s*"(\w+)"\s*,\s*"([\w-]+)"\s*,\s*"([\w/-]+)"\s*,\s*"(https?://[^"]+)"'
)


def _public_routes() -> list[tuple[str, str, str, str]]:
    """(env, route_name, service_id, host_subdomain) for public-route targets."""
    rows: list[tuple[str, str, str, str]] = []
    for env, name, service_id, url in _ROW_RE.findall(
        WORKER.read_text(encoding="utf-8")
    ):
        if not name.endswith("-public-route"):
            continue
        host = url.split("://", 1)[1].split("/", 1)[0]
        subdomain = host.split(".", 1)[0]  # `minio` / `minio-staging` / `sso`
        rows.append((env, name, service_id, subdomain))
    return rows


def _subdomain_to_meta() -> dict:
    """public subdomain -> ServiceMeta (invert registry.subdomains())."""
    attrs = service_registry.service_attrs()
    by_id = {m.service_id: m for m in attrs.values()}
    return {sub: by_id[sid] for sid, sub in service_registry.subdomains().items()}


def test_staging_public_routes_are_not_prod_only_services() -> None:
    """A prod_only service is a single shared instance with no per-env host, so a `staging`
    public-route to it targets a domain that never resolves -> a permanent false alert."""
    routes = _public_routes()
    assert routes, "no public-route targets parsed from worker.js (parser drift?)"

    by_sub = _subdomain_to_meta()
    problems: list[str] = []
    checked = 0
    for env, name, service_id, subdomain in routes:
        base = subdomain.removesuffix("-staging")
        meta = by_sub.get(base)
        if meta is None:
            continue  # bootstrap route (dokploy/vault) — outside the platform registry scan
        checked += 1
        if service_id != meta.service_id:
            problems.append(
                f"{env} {name}: service_id={service_id} but registry={meta.service_id}"
            )
        if env != "production" and meta.prod_only:
            problems.append(
                f"{env} {name}: '{meta.service_id}' is prod_only (single shared instance) — a "
                f"non-production public route targets a host that never exists (infra2#307)"
            )

    assert checked, "no public routes resolved to registry services (mapping drift?)"
    assert not problems, "public route env disagrees with registry prod_only:\n" + "\n".join(
        problems
    )
