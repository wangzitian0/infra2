"""Canonical service identity shared by deploy, runtime, telemetry, and alerts."""

from __future__ import annotations

import re
from dataclasses import dataclass

IDENTITY_SCHEMA_VERSION = "v1"
MANAGED_BY = "infra2"
DOCKER_LABEL_PREFIX = "party.zitian.infra"

_SERVICE_ID_RE = re.compile(r"^[a-z0-9_]+/[a-z0-9][a-z0-9_-]*$")
_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_ENVIRONMENT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class ServiceIdentity:
    """One versioned identity rendered into every operational plane."""

    service_id: str
    environment: str
    component: str
    service_name: str
    version: str = ""
    iac_ref: str = ""

    @classmethod
    def build(
        cls,
        service_id: str,
        environment: str,
        *,
        component: str | None = None,
        service_name: str | None = None,
        version: str = "",
        iac_ref: str = "",
    ) -> "ServiceIdentity":
        canonical_id = (service_id or "").strip()
        if not _SERVICE_ID_RE.fullmatch(canonical_id):
            raise ValueError(
                "service_id must be '<namespace>/<service>' using lowercase "
                "letters, numbers, '_' or '-'"
            )
        namespace, service = canonical_id.split("/", 1)
        canonical_environment = (environment or "").strip().lower()
        if not _ENVIRONMENT_RE.fullmatch(canonical_environment):
            raise ValueError("environment must be a non-empty lowercase identity token")
        canonical_component = _canonical_token(component or service, "component")
        canonical_service_name = _canonical_token(
            service_name or service.replace("_", "-"), "service_name"
        )
        canonical_version = _optional_value(version, "version")
        canonical_iac_ref = (iac_ref or "").strip().lower()
        if canonical_iac_ref and not _SHA40_RE.fullmatch(canonical_iac_ref):
            raise ValueError("iac_ref must be an exact 40-character commit SHA")
        return cls(
            service_id=canonical_id,
            environment=canonical_environment,
            component=canonical_component,
            service_name=canonical_service_name,
            version=canonical_version,
            iac_ref=canonical_iac_ref,
        )

    @property
    def namespace(self) -> str:
        return self.service_id.split("/", 1)[0].replace("_", "-")

    def deploy_env(self) -> dict[str, str]:
        """Dokploy/Compose env signed by the deployment control plane."""
        values = {
            "INFRA_IDENTITY_SCHEMA": IDENTITY_SCHEMA_VERSION,
            "INFRA_MANAGED_BY": MANAGED_BY,
            "INFRA_SERVICE_ID": self.service_id,
            "INFRA_SERVICE_NAMESPACE": self.namespace,
            "INFRA_SERVICE_NAME": self.service_name,
            "INFRA_COMPONENT": self.component,
            "INFRA_ENVIRONMENT": self.environment,
        }
        if self.version:
            values["INFRA_SERVICE_VERSION"] = self.version
        if self.iac_ref:
            values["INFRA_IAC_REF"] = self.iac_ref
        return values

    def otel_resource_attributes(
        self, *, include_legacy_environment: bool = True
    ) -> str:
        """Stable OTEL_RESOURCE_ATTRIBUTES string using semantic-convention keys."""
        attributes = {
            "deployment.environment.name": self.environment,
            "infra.component": self.component,
            "infra.iac.ref": self.iac_ref,
            "infra.identity.schema": IDENTITY_SCHEMA_VERSION,
            "infra.managed_by": MANAGED_BY,
            "infra.service.id": self.service_id,
            "service.name": self.service_name,
            "service.namespace": self.namespace,
            "service.version": self.version,
        }
        if include_legacy_environment:
            attributes["deployment.environment"] = self.environment
        return ",".join(
            f"{key}={value}" for key, value in sorted(attributes.items()) if value
        )

    def docker_labels(self) -> dict[str, str]:
        """Reverse-DNS container labels corresponding to this identity."""
        values = {
            "identity-schema": IDENTITY_SCHEMA_VERSION,
            "managed-by": MANAGED_BY,
            "service-id": self.service_id,
            "service-namespace": self.namespace,
            "service-name": self.service_name,
            "component": self.component,
            "environment": self.environment,
            "service-version": self.version,
            "iac-ref": self.iac_ref,
        }
        return {
            f"{DOCKER_LABEL_PREFIX}.{key}": value
            for key, value in values.items()
            if value
        }

    def alert_labels(
        self, *, severity: str, failure_domain: str = ""
    ) -> dict[str, str]:
        """Low-cardinality Alertmanager labels for routing and deduplication."""
        labels = {
            "identity_schema": IDENTITY_SCHEMA_VERSION,
            "managed_by": MANAGED_BY,
            "service_id": self.service_id,
            "service_namespace": self.namespace,
            "service": self.service_name,
            "component": self.component,
            "environment": self.environment,
            "severity": _canonical_token(severity, "severity"),
            "failure_domain": _optional_token(failure_domain, "failure_domain"),
            "service_version": self.version,
            "iac_ref": self.iac_ref,
        }
        return {key: value for key, value in labels.items() if value}


def _canonical_token(value: str, field: str) -> str:
    token = (value or "").strip().lower().replace("_", "-")
    if not _TOKEN_RE.fullmatch(token):
        raise ValueError(f"{field} must be a lowercase operational identity token")
    return token


def _optional_token(value: str, field: str) -> str:
    return _canonical_token(value, field) if (value or "").strip() else ""


def _optional_value(value: str, field: str) -> str:
    candidate = (value or "").strip().lower()
    if any(char in candidate for char in (",", "\n", "\r")):
        raise ValueError(f"{field} must not contain commas or newlines")
    return candidate
