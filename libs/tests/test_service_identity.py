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
    assert normalize_env_name("pr-123") == "pr_123"
    assert normalize_env_name("preview-web") == "preview_web"


VALID_DOKPLOY_DYNAMIC_ALIASES = [
    "pr-1",
    "pr-9999",
    "commit-a1b2c3d4e5f6",
    "branch-feature-login",
    "branch-feat-123",
    "tag-v1.0.0",
    "tag-2.1.0",
    "preview-service",
    "prod",
    "stg",
]

INVALID_DIRTY_ENV_INPUTS = [
    "",
    "   ",
    None,
    123,
    "invalid-alias",
    "unknown-env",
    "dev",
    "feature/xxx",
    "local",
]

STRICT_MODE_STATEFUL_ENVS = [
    "preview",
    "staging",
    "production",
]


@pytest.mark.parametrize("env_name", VALID_DOKPLOY_DYNAMIC_ALIASES)
def test_dokploy_dynamic_aliases_are_stateful_in_non_strict(env_name: str) -> None:
    from libs.common import is_stateful_deploy_env
    from libs.service_identity import is_stateful_deploy_env as si_is_stateful

    assert is_stateful_deploy_env(env_name, strict=False) is True
    assert si_is_stateful(env_name, strict=False) is True


@pytest.mark.parametrize("bad_env", INVALID_DIRTY_ENV_INPUTS)
def test_invalid_and_dirty_env_inputs_rejected(bad_env) -> None:
    from libs.common import is_stateful_deploy_env
    from libs.service_identity import is_stateful_deploy_env as si_is_stateful

    assert is_stateful_deploy_env(bad_env, strict=False) is False
    assert si_is_stateful(bad_env, strict=False) is False


@pytest.mark.parametrize("env_name", STRICT_MODE_STATEFUL_ENVS)
def test_strict_mode_accepts_only_canonical_stateful_envs(env_name: str) -> None:
    from libs.common import is_stateful_deploy_env
    from libs.service_identity import is_stateful_deploy_env as si_is_stateful

    assert is_stateful_deploy_env(env_name, strict=True) is True
    assert si_is_stateful(env_name, strict=True) is True


@pytest.mark.parametrize("alias", VALID_DOKPLOY_DYNAMIC_ALIASES)
def test_strict_mode_rejects_all_dynamic_aliases(alias: str) -> None:
    from libs.common import is_stateful_deploy_env
    from libs.service_identity import is_stateful_deploy_env as si_is_stateful

    assert is_stateful_deploy_env(alias, strict=True) is False
    assert si_is_stateful(alias, strict=True) is False


def test_service_identity_and_common_env_convergence(monkeypatch) -> None:
    import os
    from libs.common import get_env, set_deploy_env, reset_env_cache

    monkeypatch.setenv("INFRA_ENVIRONMENT", "staging")
    monkeypatch.delenv("DEPLOY_ENV", raising=False)
    reset_env_cache()
    env = get_env()
    assert env["ENV_SUFFIX"] == "-staging"

    set_deploy_env("production")
    assert os.environ["INFRA_ENVIRONMENT"] == "production"
    assert os.environ["DEPLOY_ENV"] == "production"

