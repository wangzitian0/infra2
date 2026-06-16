"""Infra-013: the service registry derives from deploy.py, and the
hand-maintained service lists must stay equal to it (fail-closed on drift)."""

from __future__ import annotations

import ast
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


def test_sync_runner_all_services_is_derivable() -> None:
    """sync_runner.ALL_SERVICES is a hand-copied list of the registry — it must
    equal the derived set, so adding a service can't silently omit fan-out."""
    hardcoded = _all_services_literal()
    assert set(hardcoded) == set(reg.all_services()), (
        "ALL_SERVICES in bootstrap/06.iac_runner/sync_runner.py drifted from the "
        "service registry; regenerate it from libs.service_registry.all_services()"
    )


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


def _all_services_literal() -> list[str]:
    """Extract the ALL_SERVICES list literal from sync_runner.py via AST."""
    source = (REPO_ROOT / "bootstrap/06.iac_runner/sync_runner.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "ALL_SERVICES" for t in node.targets
        ):
            value = node.value
            assert isinstance(value, (ast.List, ast.Tuple)), (
                "ALL_SERVICES must remain a list/tuple literal of string constants "
                "for this audit to verify it statically against the registry; it is "
                f"now a {type(value).__name__}. If the shape changed intentionally, "
                "update _all_services_literal() to match."
            )
            return [elt.value for elt in value.elts if isinstance(elt, ast.Constant)]
    raise AssertionError("ALL_SERVICES not found in sync_runner.py")
