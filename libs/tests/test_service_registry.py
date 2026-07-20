"""Infra-013: the service registry derives from deploy.py, and the
hand-maintained service lists must stay equal to it (fail-closed on drift)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from libs import service_registry as reg
from libs.deploy.deployer import discover_services

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_reader_extracts_known_deployer_facts() -> None:
    """Golden check: the AST reader sees the attributes services declare."""
    attrs = reg.service_attrs()

    signoz = attrs["platform/signoz"]
    assert signoz.prod_only is True
    # Infra-014 follow-up: SigNoz routing is Dokploy-managed again. The base flow
    # registers the Web UI domain from subdomain="signoz", and deploy.py registers the
    # second otel-collector ingest domain via an extra ensure_domains call. No
    # hand-written Traefik labels, so domain generation stays enabled.
    assert signoz.subdomain == "signoz"

    authentik = attrs["platform/authentik"]
    assert authentik.prod_only is False
    assert authentik.subdomain == "sso"

    postgres = attrs["platform/postgres"]
    assert postgres.prod_only is False
    assert postgres.subdomain is None

    app = attrs["finance_report/app"]
    assert app.telemetry_service_name == "finance-report-backend"
    assert app.telemetry_component == "backend"
    # finance_report has no dedicated domain — it stays on the shared INTERNAL_DOMAIN
    # a deploy request passes in, unlike truealpha below.
    assert app.domain is None

    truealpha_app = attrs["truealpha/app"]
    # TrueAlpha is an independent product with its own registered domain, not a
    # shared-platform service (Infra-021: truealpha.club, not zitian.party).
    assert truealpha_app.domain == "truealpha.club"


def test_domain_for_service_returns_override_or_none() -> None:
    assert reg.domain_for_service("truealpha/app") == "truealpha.club"
    assert reg.domain_for_service("finance_report/app") is None
    assert reg.domain_for_service("platform/does-not-exist") is None


def test_registry_builds_cross_plane_identity_from_deployer_facts() -> None:
    identity = reg.service_identity(
        "finance_report/app",
        "staging",
        component="backend",
        version="abc1234",
        iac_ref="a" * 40,
    )

    assert identity.service_id == "finance_report/app"
    assert identity.namespace == "finance-report"
    assert identity.service_name == "finance-report-backend"
    assert identity.component == "backend"


def test_monitoring_components_resolve_to_canonical_service_ids() -> None:
    assert reg.service_id_for_component("alerting") == "platform/alerting"
    assert reg.service_id_for_component("vault") == "bootstrap/vault"
    assert reg.service_id_for_component("finance-report-api") == "finance_report/app"
    assert (
        reg.service_id_for_component("postgres", signal="platform-postgres-tcp")
        == "platform/postgres"
    )

    with pytest.raises(ValueError, match="does not resolve"):
        reg.service_id_for_component("postgres")


def test_dokploy_coordinates_resolve_inside_project_namespace() -> None:
    assert reg.service_id_for_dokploy("platform", "postgres") == "platform/postgres"
    assert (
        reg.service_id_for_dokploy("finance_report", "postgres")
        == "finance_report/postgres"
    )
    assert reg.service_id_for_dokploy("bootstrap", "vault") == "bootstrap/vault"
    assert reg.service_id_for_dokploy("manual", "postgres") is None


def test_all_services_matches_discover_services() -> None:
    """The registry enumerates exactly the deploy.py-backed services."""
    assert set(reg.all_services()) == set(discover_services())


def test_sync_runner_service_set_is_derived_not_hand_listed() -> None:
    """sync_runner must DERIVE its service set (Infra-013), never hand-maintain it. Statically
    assert there is NO module-level ALL_SERVICES / SERVICE_TASK_MAP assignment and that the
    lazy accessors exist — so reintroducing a hardcoded list fails this test (the drift guard)."""
    tree = ast.parse(
        (REPO_ROOT / "bootstrap/06.iac_runner/sync_runner.py").read_text(
            encoding="utf-8"
        )
    )
    module_assignments = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "ALL_SERVICES" not in module_assignments, (
        "sync_runner reintroduced a hand-list"
    )
    assert "SERVICE_TASK_MAP" not in module_assignments, (
        "sync_runner reintroduced a hand-list"
    )
    functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {"_all_services", "_service_task_map"} <= functions


def test_shared_services_are_the_prod_only_set() -> None:
    """shared (single-instance) services are exactly the prod_only ones."""
    assert reg.shared_services() == {
        "platform/signoz",
        "platform/clickhouse",
        "platform/openpanel",
    }


def test_services_in_env_excludes_prod_only_off_production() -> None:
    staging = set(reg.services_in_env("staging"))
    production = set(reg.services_in_env("production"))

    assert reg.shared_services() & staging == set()
    assert reg.shared_services() <= production
    assert production == set(reg.all_services())
