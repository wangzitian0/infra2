"""Enforce that the rendered INFRA_PROBE_SPECS agrees with the service registry
on the facts they SHARE.

History: Infra-013 P1 first REJECTED generating the probe specs (the per-probe
truth — health paths, expected codes, severities, cascade depends_on, command
round-trips — had no owner-side home), and instead made the registry the
enforced source for the one shared fact: a service's `prod_only` identity,
which decides whether its probe target carries `${ENV_SUFFIX}`. #541 gave the
per-probe truth a home (ProbeFacet declarations on each owning Deployer), so
the specs ARE now generated — from the same registry — and this audit keeps
the shared-fact rule pointed at the rendered output.

The trap is unchanged: a prod_only service (single shared instance, no per-env
copy) probed with `${ENV_SUFFIX}` targets a host that never exists -> a
*permanent false-positive alert*. Its inverse — a per-env service probed
WITHOUT the suffix — only ever checks prod and silently ignores staging. Both
stay registry-derived, not hand-vigilance.
"""

from __future__ import annotations

from libs import service_registry
from libs.probe_specs import render_probe_spec_text

_SUFFIX = "${ENV_SUFFIX}"


def _probe_spec_lines() -> list[str]:
    """The rendered `name|kind|target|...` rows (registry-derived)."""
    return [
        line.strip()
        for line in render_probe_spec_text().splitlines()
        if line.strip() and "|" in line
    ]


def _target_host(kind: str, target: str) -> str:
    """Container hostname a probe points at (keeps any literal ${ENV_SUFFIX})."""
    netloc = target.split("://", 1)[-1]  # drop http(s):// if present
    netloc = netloc.split("/", 1)[0]  # drop path
    return netloc.split(":", 1)[0]  # drop :port (suffix has no colon)


def test_infra_probe_specs_env_suffix_matches_registry_prod_only() -> None:
    rows = _probe_spec_lines()
    assert rows, "rendered INFRA_PROBE_SPECS empty (renderer drift?)"

    problems: list[str] = []
    checked = 0
    for row in rows:
        parts = row.split("|")
        name, kind, target = parts[0], parts[1], parts[2]
        if kind in ("command", "resource"):
            continue  # round-trips/host backstops don't target a registry container
        host = _target_host(kind, target)
        # resolve_container_host handles sub-container names (signoz-otel-collector ->
        # signoz) via longest -prefix, so the enforcement isn't silently skipped for them.
        meta = service_registry.resolve_container_host(host)
        if meta is None:
            continue  # bootstrap svc (dokploy/vault/...) — outside the platform registry scan
        checked += 1
        has_suffix = _SUFFIX in host
        if meta.prod_only and has_suffix:
            problems.append(
                f"{name}: prod_only service '{meta.service_id}' must NOT use {_SUFFIX} "
                f"(single shared instance; -staging targets a host that never exists)"
            )
        if not meta.prod_only and not has_suffix:
            problems.append(
                f"{name}: per-env service '{meta.service_id}' should use {_SUFFIX} "
                f"(else only prod is probed and staging is silently unmonitored)"
            )

    assert checked, "no probe targets resolved to registry services (mapping drift?)"
    assert not problems, (
        "probe env-suffix disagrees with registry prod_only:\n" + "\n".join(problems)
    )
