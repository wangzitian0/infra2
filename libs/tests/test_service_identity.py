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
