"""Each SDK container with a deploy identity receives SERVICE_VERSION (infra2#1823 N1).

The locked infra2-sdk reads the service version only from SERVICE_VERSION; it does not
read the retired alias IMAGE_TAG. Each deploy path (sync pipeline, promote, preview)
issues INFRA_SERVICE_VERSION and OTEL_RESOURCE_ATTRIBUTES from one ServiceIdentity. A
container that receives the issued OTEL_RESOURCE_ATTRIBUTES (or the worker attributes)
must also set ``SERVICE_VERSION: ${INFRA_SERVICE_VERSION:-}``. A rollback replaces
INFRA_SERVICE_VERSION, so SERVICE_VERSION then names the image that runs.

Scope: every tracked compose file, compiled. The vault-agent sidecar runs no SDK code.
"""

from __future__ import annotations

import pytest
import yaml

from common.deploy.compose_compile import load_stack
from common.deploy.compose_equivalence import tracked_compose_files

ISSUED_ATTRIBUTES = (
    "${OTEL_RESOURCE_ATTRIBUTES:-}",
    "${OTEL_WORKER_RESOURCE_ATTRIBUTES:-}",
)
SERVICE_VERSION = "${INFRA_SERVICE_VERSION:-}"


def _environment(spec: dict) -> dict[str, str]:
    env = spec.get("environment") or {}
    if isinstance(env, list):
        return dict(item.split("=", 1) if "=" in item else (item, "") for item in env)
    return {key: "" if value is None else str(value) for key, value in env.items()}


def identity_services(text: str) -> dict[str, str | None]:
    """Each service of ``text`` that receives the issued attributes, with its SERVICE_VERSION."""
    doc = yaml.safe_load(text) or {}
    found: dict[str, str | None] = {}
    for name, spec in (doc.get("services") or {}).items():
        if name == "vault-agent" or not isinstance(spec, dict):
            continue
        env = _environment(spec)
        if env.get("OTEL_RESOURCE_ATTRIBUTES") in ISSUED_ATTRIBUTES:
            found[name] = env.get("SERVICE_VERSION")
    return found


def _compiled() -> dict[str, str]:
    return {path: load_stack(path).compiled for path in tracked_compose_files()}


def test_scope_is_not_empty() -> None:
    services = {
        f"{path}:{name}"
        for path, text in _compiled().items()
        for name in identity_services(text)
    }
    assert len(services) >= 11, sorted(services)


@pytest.mark.parametrize("path", tracked_compose_files())
def test_every_identity_service_sets_service_version(path: str) -> None:
    services = identity_services(load_stack(path).compiled)
    assert {n: v for n, v in services.items() if v != SERVICE_VERSION} == {}


def test_guard_rejects_a_service_without_service_version() -> None:
    text = (
        "services:\n  app:\n    environment:\n"
        "      OTEL_RESOURCE_ATTRIBUTES: ${OTEL_RESOURCE_ATTRIBUTES:-}\n"
    )
    assert identity_services(text) == {"app": None}


def test_guard_rejects_the_retired_image_tag_as_the_value() -> None:
    text = (
        "services:\n  worker:\n    environment:\n"
        "      - OTEL_RESOURCE_ATTRIBUTES=${OTEL_WORKER_RESOURCE_ATTRIBUTES:-}\n"
        "      - SERVICE_VERSION=${IMAGE_TAG}\n"
    )
    assert identity_services(text) == {"worker": "${IMAGE_TAG}"}


def test_guard_skips_the_vault_agent_and_fixed_attributes() -> None:
    text = (
        "services:\n"
        "  vault-agent:\n    environment:\n"
        "      OTEL_RESOURCE_ATTRIBUTES: ${OTEL_RESOURCE_ATTRIBUTES:-}\n"
        "  collector:\n    environment:\n"
        "      OTEL_RESOURCE_ATTRIBUTES: host.name=collector\n"
    )
    assert identity_services(text) == {}
