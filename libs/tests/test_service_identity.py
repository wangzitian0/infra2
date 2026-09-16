"""Infra-013 cross-plane service identity contract."""

import pytest

from libs.service_identity import ServiceIdentity


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
        "INFRA_SERVICE_NAMESPACE": "finance-report",
        "INFRA_SERVICE_NAME": "finance-report-backend",
        "INFRA_COMPONENT": "backend",
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
    from libs.common import (
        DEPLOYMENT_ENV_PREVIEW,
        DEPLOYMENT_ENV_PRODUCTION,
        DEPLOYMENT_ENV_STAGING,
        STATEFUL_DEPLOY_ENVIRONMENTS,
        is_stateful_deploy_env,
        normalize_env_name,
    )
    from libs.service_identity import (
        STATEFUL_DEPLOY_ENVIRONMENTS as SI_STATEFUL_ENVIRONMENTS,
        is_stateful_deploy_env as si_is_stateful,
    )

    # Exactly 3 stateful deployment environments
    assert STATEFUL_DEPLOY_ENVIRONMENTS == ("preview", "staging", "production")
    assert SI_STATEFUL_ENVIRONMENTS == STATEFUL_DEPLOY_ENVIRONMENTS
    assert DEPLOYMENT_ENV_PREVIEW == "preview"
    assert DEPLOYMENT_ENV_STAGING == "staging"
    assert DEPLOYMENT_ENV_PRODUCTION == "production"

    # Stateful recognition
    for env in ("preview", "staging", "production", "prod", "stg", "PROD", "Staging"):
        assert is_stateful_deploy_env(env) is True
        assert si_is_stateful(env) is True

    # Preview dynamic instances per deploy_env_config.preview_alias SSOT model
    for preview_instance in (
        "pr-42",
        "commit-1ab32d5",
        "branch-feature-x",
        "tag-v1-2-3",
        "preview-pr-12",
    ):
        assert is_stateful_deploy_env(preview_instance) is True
        assert is_stateful_deploy_env(preview_instance, strict=True) is False

    # Stateless runners / ephemeral test runners are NOT stateful deployment environments
    for runner in ("local", "dev", "github-actions", "runner", "ci", "", None):
        assert is_stateful_deploy_env(runner) is False

    # Normalization preserves canonical tiers
    assert normalize_env_name("prod") == "production"
    assert normalize_env_name("stg") == "staging"
    assert normalize_env_name("staging") == "staging"
    assert normalize_env_name("preview") == "preview"
