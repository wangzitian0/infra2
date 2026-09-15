"""Every vault-agent sidecar carries a cache-bust in its own `environment:` block (#628,
truealpha#447 / infra2#562-followup).

secrets.ctmpl is a single-file bind mount: a checkout that rewrites the file gives it a
new inode, the running agent keeps the old one, and Compose recreates a service only when
its resolved config changes. The Deployer folds every `./`-relative bind mount — the
template included — into IAC_CONFIG_HASH, so an agent that reads the variable is recreated
by exactly the deploy that changed its template. An agent that does not read it ships every
template edit as a silent no-op: on 2026-09-15 thirteen agents fleet-wide (created in
June/July) still rendered June's templates while the file on disk matched HEAD; on 2026-09-07
truealpha/data_engine staging rendered nothing for the `LLM_*` keys v1.1.56 "deployed".

The promote tier (libs.deploy.promote.deploy()) additionally sets IAC_CONFIG_HASH fresh on
every call so a same-digest promote is never a Dokploy no-op — which only holds for the app
services that read it too (truealpha-app-vault-agent-staging kept rendering a template
without SECRET_KEY across two full redeploys until the variable was added).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_ROOTS = ("bootstrap", "platform", "finance_report", "truealpha")

# Either the Deployer's hash or a service's own configuration digest recreates the agent.
CACHE_BUST_VARIABLES = ("IAC_CONFIG_HASH", "TRUEALPHA_CONFIGURATION_SHA256")

# Promote-tier app compose files: the ones libs.deploy.promote.deploy() governs for
# finance_report/app and truealpha/app, fixed envs and preview alike. Their app services
# source the rendered file at boot, so they must be recreated alongside the agent.
PROMOTE_TIER_COMPOSE_FILES = (
    "finance_report/finance_report/10.app/compose.yaml",
    "finance_report/finance_report/preview/compose.yaml",
    "truealpha/truealpha/10.app/compose.yaml",
    "truealpha/truealpha/preview/compose.yaml",
)


def _services(compose_path: Path) -> dict[str, dict]:
    doc = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
    return {
        name: spec
        for name, spec in (doc.get("services") or {}).items()
        if isinstance(spec, dict)
    }


def _mounts(spec: dict, needle: str) -> bool:
    return any(isinstance(v, str) and needle in v for v in spec.get("volumes") or [])


def _vault_agent_composes() -> list[str]:
    """Every compose whose vault-agent bind-mounts secrets.ctmpl, repo-relative."""
    found = []
    for top in COMPOSE_ROOTS:
        for compose in sorted((ROOT / top).rglob("compose.yaml")):
            agent = _services(compose).get("vault-agent")
            if agent and _mounts(agent, "secrets.ctmpl"):
                found.append(str(compose.relative_to(ROOT)))
    return found


VAULT_AGENT_COMPOSE_FILES = _vault_agent_composes()


def test_every_vault_agent_is_discovered():
    assert len(VAULT_AGENT_COMPOSE_FILES) >= 16, VAULT_AGENT_COMPOSE_FILES
    assert set(PROMOTE_TIER_COMPOSE_FILES) <= set(VAULT_AGENT_COMPOSE_FILES)
    assert "bootstrap/06.iac_runner/compose.yaml" in VAULT_AGENT_COMPOSE_FILES


@pytest.mark.parametrize("relative_path", VAULT_AGENT_COMPOSE_FILES)
def test_vault_agent_is_recreated_when_its_template_changes(relative_path):
    environment = (
        _services(ROOT / relative_path)["vault-agent"].get("environment") or {}
    )
    assert any(name in environment for name in CACHE_BUST_VARIABLES), (
        f"{relative_path}: vault-agent reads none of {CACHE_BUST_VARIABLES}, so a "
        "secrets.ctmpl edit never recreates it and ships as a silent no-op (#628). Add "
        "`IAC_CONFIG_HASH: ${IAC_CONFIG_HASH:-}` to its `environment:` block."
    )


@pytest.mark.parametrize("relative_path", VAULT_AGENT_COMPOSE_FILES)
def test_vault_agent_forwards_every_env_its_template_reads(relative_path):
    """`env "X"` in secrets.ctmpl reads the agent container's own environment, not the
    Compose project env. #604 added `env "TA_MINIO_S3_PORT"` to a template while the
    compose kept forwarding only TA_POSTGRES_PORT, so a recreated agent rendered
    S3_ENDPOINT=http://127.0.0.1: (no port) and every raw capture failed (2026-09-07)."""
    compose = ROOT / relative_path
    template = (compose.parent / "secrets.ctmpl").read_text(encoding="utf-8")
    referenced = set(re.findall(r'env "([A-Za-z0-9_]+)"', template))
    forwarded = set(_services(compose)["vault-agent"].get("environment") or {})
    missing = sorted(referenced - forwarded)
    assert not missing, (
        f"{relative_path}: secrets.ctmpl reads {missing} but vault-agent does not forward "
        "them — the agent would render them empty"
    )


@pytest.mark.parametrize("relative_path", PROMOTE_TIER_COMPOSE_FILES)
def test_promote_tier_secret_consumers_carry_iac_config_hash(relative_path):
    services = _services(ROOT / relative_path)
    consumers = {
        name: spec
        for name, spec in services.items()
        if _mounts(spec, "secrets.ctmpl") or _mounts(spec, "secrets:/secrets:ro")
    }
    assert consumers, (
        f"{relative_path}: expected at least one service mounting secrets.ctmpl or "
        "the rendered secrets volume — did the compose shape change?"
    )
    missing = sorted(
        name
        for name, spec in consumers.items()
        if "IAC_CONFIG_HASH" not in (spec.get("environment") or {})
    )
    assert not missing, (
        f"{relative_path}: service(s) {missing} render/consume Vault-templated "
        "secrets but don't read IAC_CONFIG_HASH, so libs.deploy.promote.deploy()'s "
        "per-call cache-bust can't force Compose to recreate them on a same-tag "
        "redeploy — a secrets.ctmpl change can silently never reach a running "
        "staging/prod stack (truealpha#447). Add "
        "`IAC_CONFIG_HASH: ${IAC_CONFIG_HASH:-}` to its `environment:` block."
    )
