"""#542 SecretsFacet migration equivalence proof — PERMANENT regression anchor.

``libs/tests/fixtures/vault_self_refresh_inventory_frozen.yaml`` is a byte-copy
of the handwritten ``vault-self-refresh-inventory.yaml`` SSOT as it stood
before the facet migration deleted it (16 entries, comments and all). The facet-derived
inventory (each service's ``secrets = (SecretsFacet(...),)`` declarations ->
``service_attrs()``/``bootstrap_facet_attrs()`` -> ``load_inventory()``) must
stay FIELD-IDENTICAL to it. This is not a one-time diff: the frozen fixture
anchors the audit's expectations forever, so a facet edit that accidentally
mutates an inventory fact fails here with an exact field diff.

Deliberate inventory changes must update the fixture in the same PR, making the
change explicit and reviewable (the fixture is the inventory changelog).

The counterfactual tests below are the issue's AC: editing a Deployer's secrets
facet (or the deploy facts it derives from) must change BOTH the derived audit
expectation AND the deploy-side value built from the same fact, in lockstep —
the structural fix for #531's root cause (audit expectations and deployment
drifting apart).
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest
import yaml

from libs import service_registry as reg
from libs.service_facets import SecretsFacet
from libs.vault_self_refresh_audit import (
    VaultService,
    _vault_service_from_facet,
    inventory_ids_not_in_production,
    load_inventory,
    vault_path_template,
)

ROOT = Path(__file__).resolve().parents[2]
FROZEN = ROOT / "libs/tests/fixtures/vault_self_refresh_inventory_frozen.yaml"

# The two fields ADDED by #542 (facts absorbed from the audit module's former
# MOUNT_EXEMPT_CONTAINERS / OPTIONAL_INERT_FIELD_WATCHLIST constants) — they
# have no column in the frozen pre-migration YAML, so they are equivalence-
# checked against the retired constants' content separately below.
_POST_MIGRATION_FIELDS = {"mount_exempt_containers", "optional_inert_fields"}


def _frozen_inventory() -> list[VaultService]:
    """Parse the frozen fixture with the retired YAML loader's exact merge
    semantics (defaults overlaid per service, app_containers tupled)."""
    data = yaml.safe_load(FROZEN.read_text(encoding="utf-8"))
    defaults = data.get("defaults", {})
    services: list[VaultService] = []
    for raw_service in data.get("services", []):
        merged = {**defaults, **raw_service}
        merged["app_containers"] = tuple(merged.get("app_containers", ()))
        services.append(VaultService(**merged))
    return services


def test_derived_inventory_equals_frozen_yaml_field_by_field() -> None:
    """THE equivalence proof: derived == frozen, per service, per field."""
    derived = {service.id: service for service in load_inventory()}
    frozen = {service.id: service for service in _frozen_inventory()}

    assert sorted(derived) == sorted(frozen), (
        "vault inventory ID set diverged from the frozen YAML.\n"
        f"missing from derived: {sorted(set(frozen) - set(derived))}\n"
        f"new in derived:       {sorted(set(derived) - set(frozen))}\n"
        "If this change is deliberate, update libs/tests/fixtures/"
        "vault_self_refresh_inventory_frozen.yaml in the same PR."
    )
    for service_id, frozen_service in frozen.items():
        for field in dataclasses.fields(VaultService):
            if field.name in _POST_MIGRATION_FIELDS:
                continue
            assert getattr(derived[service_id], field.name) == getattr(
                frozen_service, field.name
            ), (
                f"{service_id}.{field.name} diverged from the frozen YAML:\n"
                f"  frozen:  {getattr(frozen_service, field.name)!r}\n"
                f"  derived: {getattr(derived[service_id], field.name)!r}"
            )


def test_absorbed_constants_equivalence() -> None:
    """The two retired audit-module constants live on as facet fields with the
    exact same content:
      MOUNT_EXEMPT_CONTAINERS == {platform-prefect-worker, -staging} — one
        symbolic ${ENV_SUFFIX} entry on platform/prefect resolves to exactly
        those two names across the audit's production/staging runs;
      OPTIONAL_INERT_FIELD_WATCHLIST == (finance_report/app,
        LLM_ENCRYPTION_KEYS)."""
    from libs.vault_self_refresh_audit import _resolve_env_suffix

    by_id = {service.id: service for service in load_inventory()}
    assert by_id["platform/prefect"].mount_exempt_containers == (
        "platform-prefect-worker${ENV_SUFFIX}",
    )
    resolved = {
        _resolve_env_suffix(name, env)
        for env in ("production", "staging")
        for name in by_id["platform/prefect"].mount_exempt_containers
    }
    assert resolved == {"platform-prefect-worker", "platform-prefect-worker-staging"}
    exempt_elsewhere = {
        sid: svc.mount_exempt_containers
        for sid, svc in by_id.items()
        if svc.mount_exempt_containers and sid != "platform/prefect"
    }
    assert exempt_elsewhere == {}

    assert by_id["finance_report/app"].optional_inert_fields == (
        "LLM_ENCRYPTION_KEYS",
    )
    inert_elsewhere = {
        sid: svc.optional_inert_fields
        for sid, svc in by_id.items()
        if svc.optional_inert_fields and sid != "finance_report/app"
    }
    assert inert_elsewhere == {}


def test_not_in_production_derivation_matches_deploy_side_facts() -> None:
    """The retired NOT_YET_IN_PRODUCTION constant, now derived from deploy-side
    facts — so the set FLIPS AUTOMATICALLY as reality changes, which is the
    whole point. Verified live 2026-07-19/20: truealpha/app prod went live
    (compose_id registered in _APP_COMPOSE_OVERRIDES) and truealpha-postgres is
    running in prod, so app/preview/postgres left this set with zero audit-side
    edits; data_engine keeps its Deployer flag (no prod containers exist).

    Assert the MECHANISM's current output plus the two flip properties, not a
    frozen snapshot — a snapshot here goes stale the moment rollout state moves
    (exactly what broke the previous version of this test)."""
    excluded = inventory_ids_not_in_production()
    assert excluded == {"truealpha/data_engine"}
    # flip property 1: a prod compose_id registration removes the app AND its
    # on-behalf preview surface, with no audit-side change
    assert "truealpha/app" not in excluded
    assert "truealpha/preview" not in excluded
    # flip property 2: removing the Deployer flag (postgres, live in prod since
    # 2026-07-19) removes the service
    assert "truealpha/postgres" not in excluded


def test_duplicate_derived_inventory_ids_fail_closed() -> None:
    facet = SecretsFacet(
        service_id="finance_report/preview",  # already declared by fr/app
        compose_path="x/compose.yaml",
        vault_agent_container="x-vault-agent",
        app_containers=("x",),
        auth_method="approle",
    )
    real_attrs = dict(reg.service_attrs())  # capture BEFORE monkeypatching, or
    donor = real_attrs["platform/postgres"]  # fake_attrs recurses into itself
    clashing = dataclasses.replace(donor, secrets=donor.secrets + (facet,))

    def fake_attrs():
        attrs = dict(real_attrs)
        attrs["platform/postgres"] = clashing
        return attrs

    import libs.vault_self_refresh_audit as audit_module

    original = reg.service_attrs
    try:
        reg.service_attrs = fake_attrs  # type: ignore[assignment]
        with pytest.raises(ValueError, match="duplicate vault inventory id"):
            audit_module.load_inventory()
    finally:
        reg.service_attrs = original  # type: ignore[assignment]


# --- the issue-AC counterfactual: one facet edit moves BOTH surfaces ---------


_COUNTERFACTUAL_DEPLOY = '''
from libs.deploy.deployer import Deployer, make_tasks
from libs.service_facets import SecretsFacet


class ExampleDeployer(Deployer):
    service = "example"
    compose_path = "platform/99.example/compose.yaml"
    project = "platform"
    secrets = (
        SecretsFacet(
            vault_agent_container="platform-example-vault-agent${ENV_SUFFIX}",
            app_containers=("platform-example${ENV_SUFFIX}",),
            auth_method="approle",
        ),
    )
'''


def _derived_service(source: str) -> VaultService:
    tree = ast.parse(source)
    where = "platform/99.example/deploy.py"
    meta = reg.ServiceMeta(
        service_id="platform/example",
        layer="platform",
        service=reg._class_attr(tree, "service") or "example",
        prod_only=False,
        subdomain=None,
        service_port=None,
        service_name=None,
        telemetry_service_name=None,
        telemetry_component=None,
        project=reg._class_attr(tree, "project") or "platform",
        compose_path=reg._class_attr(tree, "compose_path"),
        secrets=reg._facet_seq(tree, "secrets", SecretsFacet, where),
    )
    return _vault_service_from_facet(meta, meta.secrets[0])


def _deploy_side_vault_path(meta_project: str, meta_service: str) -> str:
    """The path the DEPLOY side actually uses: libs.env.get_secrets(app_vars)
    stores at secret/data/{project}/{env}/{service} (VaultSecrets.path)."""
    from libs.env import get_secrets

    secrets = get_secrets(
        project=meta_project,
        service=meta_service,
        env="production",
        credential_type="app_vars",
    )
    return f"secret/data/{secrets.path}"


def test_counterfactual_secrets_facet_edit_moves_audit_and_deploy_in_lockstep() -> (
    None
):
    """Issue #542 AC: editing the Deployer's declaration changes BOTH the
    derived audit expectation AND the deploy-side rendering of the same fact,
    in lockstep — there is no second copy left to go stale.

    Two counterfactual edits:
      1. `service` attr edit — the vault path fact. The audit expectation
         (VaultService.vault_path_template) and the deploy-side secret path
         (libs.env.get_secrets' VaultSecrets.path) are built from the SAME
         (project, service) attrs, so both move identically.
      2. SecretsFacet field edit (an app container name) — the audit's
         container expectation follows the facet with no YAML to update.
    """
    base = _derived_service(_COUNTERFACTUAL_DEPLOY)
    assert base.vault_path_template == "secret/data/platform/{env}/example"
    assert base.vault_path_template == vault_path_template("platform", "example")
    # Deploy side, same fact, same construction:
    deploy_path = _deploy_side_vault_path("platform", "example")
    assert deploy_path == base.vault_path_template.format(env="production")

    # Counterfactual 1: rename the service attr on the Deployer.
    renamed = _COUNTERFACTUAL_DEPLOY.replace('service = "example"', 'service = "renamed"')
    edited = _derived_service(renamed)
    assert edited.vault_path_template == "secret/data/platform/{env}/renamed"
    assert edited.dokploy_service == "renamed"
    # ... and the deploy-side path moved WITH it (same fact, both surfaces):
    assert _deploy_side_vault_path("platform", "renamed") == edited.vault_path_template.format(
        env="production"
    )
    assert _deploy_side_vault_path("platform", "renamed") != deploy_path

    # Counterfactual 2: edit a facet field (app container name).
    recontainered = _COUNTERFACTUAL_DEPLOY.replace(
        'app_containers=("platform-example${ENV_SUFFIX}",)',
        'app_containers=("platform-example-api${ENV_SUFFIX}",)',
    )
    assert _derived_service(recontainered).app_containers == (
        "platform-example-api${ENV_SUFFIX}",
    )


def test_counterfactual_compose_path_edit_moves_audit_expectation() -> None:
    """The audit's compose/agent-config/template path expectations derive from
    the Deployer's own compose_path — the attribute the deploy actually ships —
    so moving the deploy artifact moves the audit expectation with it."""
    moved = _COUNTERFACTUAL_DEPLOY.replace(
        'compose_path = "platform/99.example/compose.yaml"',
        'compose_path = "platform/98.moved/compose.yaml"',
    )
    derived = _derived_service(moved)
    assert derived.compose_path == "platform/98.moved/compose.yaml"
    assert derived.vault_agent_config_path == "platform/98.moved/vault-agent.hcl"
    assert derived.secret_template_path == "platform/98.moved/secrets.ctmpl"
