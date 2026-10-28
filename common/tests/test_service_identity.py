"""Infra-013 cross-plane service identity contract."""

import pytest

from common.core.service_identity import ServiceIdentity


def test_identity_renders_same_coordinates_into_every_plane() -> None:
    identity = ServiceIdentity.build(
        "finance_report/app",
        "production",
        component="backend",
        service_name="finance-report-backend",
        version="ABC1234",
        iac_ref="A" * 40,
    )

    assert identity.deploy_env() == {
        "INFRA_IDENTITY_SCHEMA": "v1",
        "INFRA_MANAGED_BY": "infra2",
        "INFRA_SERVICE_ID": "finance_report/app",
        "INFRA_ENVIRONMENT": "production",
        "INFRA_SERVICE_VERSION": "abc1234",
        "INFRA_IAC_REF": "a" * 40,
    }
    otel = identity.otel_resource_attributes()
    assert "service.namespace=finance-report" in otel
    assert "service.name=finance-report-backend" in otel
    assert "deployment.environment.name=production" in otel
    assert "deployment.environment=production" in otel
    assert "infra.service.id=finance_report/app" in otel
    assert identity.docker_labels()["party.zitian.infra.service-id"] == (
        "finance_report/app"
    )
    labels = identity.alert_labels(severity="critical", failure_domain="public-route")
    assert labels["service_id"] == "finance_report/app"
    assert labels["environment"] == "production"
    assert labels["failure_domain"] == "public-route"


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"service_id": "Finance/App", "environment": "production"}, "service_id"),
        ({"service_id": "finance/app", "environment": ""}, "environment"),
        (
            {
                "service_id": "finance/app",
                "environment": "production",
                "iac_ref": "main",
            },
            "iac_ref",
        ),
        (
            {
                "service_id": "finance/app",
                "environment": "production",
                "version": "bad,value",
            },
            "version",
        ),
    ],
)
def test_identity_rejects_ambiguous_or_unserializable_coordinates(
    kwargs, match
) -> None:
    with pytest.raises(ValueError, match=match):
        ServiceIdentity.build(**kwargs)


def test_canonical_stateful_deployment_environments():
    from common.core.environ import (
        DEPLOYMENT_ENV_PREVIEW,
        DEPLOYMENT_ENV_PRODUCTION,
        DEPLOYMENT_ENV_STAGING,
        STATEFUL_DEPLOY_ENVIRONMENTS,
        normalize_env_name,
    )
    from common.core.service_identity import (
        STATEFUL_DEPLOY_ENVIRONMENTS as SI_STATEFUL_ENVIRONMENTS,
    )

    # Exactly 3 stateful deployment environments
    assert STATEFUL_DEPLOY_ENVIRONMENTS == ("preview", "staging", "production")
    assert SI_STATEFUL_ENVIRONMENTS == STATEFUL_DEPLOY_ENVIRONMENTS
    assert DEPLOYMENT_ENV_PREVIEW == "preview"
    assert DEPLOYMENT_ENV_STAGING == "staging"
    assert DEPLOYMENT_ENV_PRODUCTION == "production"

    # Normalization preserves canonical tiers
    assert normalize_env_name("prod") == "production"
    assert normalize_env_name("stg") == "staging"
    assert normalize_env_name("staging") == "staging"
    assert normalize_env_name("preview") == "preview"
    assert normalize_env_name("pr-123") == "pr_123"
    assert normalize_env_name("preview-web") == "preview_web"
    assert normalize_env_name("canary") == "canary_preview"
    assert normalize_env_name("canary-preview") == "canary_preview"
    assert normalize_env_name("canary-999") == "canary_999"
    assert normalize_env_name("branch-main") == "branch_main"
    assert normalize_env_name("commit-1a2b3c4") == "commit_1a2b3c4"
    assert normalize_env_name("tag-v1.0.0") == "tag_v1.0.0"

    with pytest.raises(ValueError, match="whitespace"):
        normalize_env_name("bad name")
    with pytest.raises(ValueError, match="whitespace"):
        normalize_env_name("bad/name")


def test_service_identity_and_common_env_convergence(monkeypatch) -> None:
    import os
    from common.core.environ import get_env, set_deploy_env, reset_env_cache

    monkeypatch.setenv("INFRA_ENVIRONMENT", "staging")
    monkeypatch.delenv("DEPLOY_ENV", raising=False)
    reset_env_cache()
    env = get_env()
    assert env["ENV_SUFFIX"] == "-staging"

    set_deploy_env("production")
    assert os.environ["INFRA_ENVIRONMENT"] == "production"
    assert os.environ["DEPLOY_ENV"] == "production"
