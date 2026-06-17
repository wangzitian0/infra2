"""Infra-013: the service registry derives from deploy.py, and the
hand-maintained service lists must stay equal to it (fail-closed on drift)."""

from __future__ import annotations

from pathlib import Path

from libs import service_registry as reg
from libs.deployer import discover_services

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


def test_all_services_matches_discover_services() -> None:
    """The registry enumerates exactly the deploy.py-backed services."""
    assert set(reg.all_services()) == set(discover_services())


def test_sync_runner_service_set_is_derived_not_hand_listed() -> None:
    """sync_runner no longer hand-maintains ALL_SERVICES / SERVICE_TASK_MAP — they are derived
    from libs.deployer.discover_services (Infra-013), so a hand-list can't drift. The two
    registry scanners (deployer.discover_services and service_registry) must agree, so the
    derivation can never silently omit a service."""
    assert set(discover_services()) == set(reg.all_services())


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


