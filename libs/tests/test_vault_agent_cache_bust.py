"""A promote-tier app's vault-agent (and app services reading its rendered secrets)
must carry IAC_CONFIG_HASH in their own `environment:` block (truealpha#447 /
infra2#562-followup).

libs.deploy.promote.deploy() (the backend deploy_v2 routes every app + staging|prod
call through) sets IAC_CONFIG_HASH fresh on every call specifically as a cache-bust
"so a same-digest promote is never a Dokploy no-op" — but that promise only holds for
a service whose OWN resolved compose config actually reads the variable. A service
that doesn't reference it (e.g. GIT_COMMIT_SHA alone, which is identical across two
deploys of the same release tag) looks unchanged to Docker Compose and is silently
never recreated by a routine redeploy — so it can run for days on a stale image-source
clone, invisible to every subsequent "successful" deploy.

This is exactly what happened to truealpha/app: secrets.ctmpl gained a SECRET_KEY line
(infra2#562) but truealpha-app-vault-agent-staging (created 8 days earlier) was never
recreated by any deploy since, so it kept rendering the OLD template with no SECRET_KEY
— app-web crash-looped on every request needing a session, unfixed by two full
redeploy cycles, until this was traced to the missing cache-bust var itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

# Promote-tier app compose files: the ones libs.deploy.promote.deploy() (not the
# platform/iac_pinned webhook path, which recreates its stack a different way) governs
# for finance_report/app and truealpha/app, fixed envs and preview alike.
PROMOTE_TIER_COMPOSE_FILES = (
    "finance_report/finance_report/10.app/compose.yaml",
    "finance_report/finance_report/preview/compose.yaml",
    "truealpha/truealpha/10.app/compose.yaml",
    "truealpha/truealpha/preview/compose.yaml",
)


def _services_reading_rendered_secrets(compose_path: Path) -> dict[str, dict]:
    """Every service that either renders (vault-agent) or sources (mounts the
    `secrets` volume read-only, i.e. `. /secrets/.env`-style) the Vault template."""
    doc = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = doc.get("services", {})
    selected = {}
    for name, spec in services.items():
        if not isinstance(spec, dict):
            continue
        volumes = spec.get("volumes") or []
        mounts_secrets_ctmpl = any(
            isinstance(v, str) and "secrets.ctmpl" in v for v in volumes
        )
        mounts_secrets_ro = any(
            isinstance(v, str) and v.startswith("secrets:/secrets:ro") for v in volumes
        )
        if mounts_secrets_ctmpl or mounts_secrets_ro:
            selected[name] = spec
    return selected


@pytest.mark.parametrize("relative_path", PROMOTE_TIER_COMPOSE_FILES)
def test_vault_secret_consumers_carry_iac_config_hash(relative_path):
    compose_path = ROOT / relative_path
    consumers = _services_reading_rendered_secrets(compose_path)
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
