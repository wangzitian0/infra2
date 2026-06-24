"""Enforce that INFRA_PROBE_SPECS agrees with the service registry on the facts they SHARE.

Infra-013 P1 framed this as "generate the downstream from the registry". On inspection the
probe specs are NOT a hand-copied service list — they carry per-probe truth the registry does
not (and should not) hold: health paths, expected codes, severities, cascade depends_on, and
`command` round-trip probes. Generating them would lose that truth. So the registry is made the
ENFORCED single source for the one fact both genuinely share: a service's `prod_only` identity,
which decides whether its probe target carries `${ENV_SUFFIX}`.

The probe file documents the trap itself: a prod_only service (single shared instance, no
per-env copy) probed with `${ENV_SUFFIX}` targets a host that never exists -> a *permanent
false-positive alert*. Its inverse — a per-env service probed WITHOUT the suffix — only ever
checks prod and silently ignores staging. Both are now registry-derived, not hand-vigilance.
"""

from __future__ import annotations

from pathlib import Path

from libs import service_registry

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "platform/12.alerting/compose.yaml"
_SUFFIX = "${ENV_SUFFIX}"


def _probe_spec_lines() -> list[str]:
    """The `name|kind|target|...` rows of the INFRA_PROBE_SPECS block-scalar."""
    lines = COMPOSE.read_text(encoding="utf-8").splitlines()
    rows: list[str] = []
    grabbing = False
    block_indent = 0
    for line in lines:
        if not grabbing:
            if "INFRA_PROBE_SPECS:" in line and line.rstrip().endswith("|"):
                grabbing = True
                block_indent = len(line) - len(line.lstrip())
            continue
        if line.strip() == "":
            continue
        if len(line) - len(line.lstrip()) <= block_indent:
            break  # dedented out of the block scalar
        row = line.strip()
        if not row.startswith("#") and "|" in row:
            rows.append(row)
    return rows


def _target_host(kind: str, target: str) -> str:
    """Container hostname a probe points at (keeps any literal ${ENV_SUFFIX})."""
    netloc = target.split("://", 1)[-1]  # drop http(s):// if present
    netloc = netloc.split("/", 1)[0]  # drop path
    return netloc.split(":", 1)[0]  # drop :port (suffix has no colon)


def test_infra_probe_specs_env_suffix_matches_registry_prod_only() -> None:
    rows = _probe_spec_lines()
    assert rows, "INFRA_PROBE_SPECS block not found / empty (parser drift?)"

    problems: list[str] = []
    checked = 0
    for row in rows:
        parts = row.split("|")
        name, kind, target = parts[0], parts[1], parts[2]
        if kind == "command":
            continue  # round-trips/canaries don't target a single registry container
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
    assert not problems, "probe env-suffix disagrees with registry prod_only:\n" + "\n".join(
        problems
    )
