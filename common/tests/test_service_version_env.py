"""Each SDK container with a deploy identity receives SERVICE_VERSION (infra2#1823 N1).

The locked infra2-sdk reads the service version only from SERVICE_VERSION; it does not
read the retired alias IMAGE_TAG. Each deploy path (sync pipeline, promote, preview)
issues INFRA_SERVICE_VERSION and OTEL_RESOURCE_ATTRIBUTES from one ServiceIdentity. A
container that receives the issued OTEL_RESOURCE_ATTRIBUTES (or the worker attributes)
must also set ``SERVICE_VERSION: ${INFRA_SERVICE_VERSION:-}``. A rollback replaces
INFRA_SERVICE_VERSION, so SERVICE_VERSION then names the image that runs.

Scope: every tracked compose file, compiled. The vault-agent sidecar runs no SDK code.

The env manifest of a stack is the contract. An App stack manifest is a projection of the
pinned App release (tools/app_manifest_sync.py). An App release with an older SDK does not
declare SERVICE_VERSION, so its stack must not set it (inventory rule G1).
"""

from __future__ import annotations

import json
from pathlib import Path

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


ROOT = Path(__file__).resolve().parents[2]


def manifest_declares_service_version(compose_path: str) -> bool:
    """True when no manifest is next to ``compose_path``, or when that manifest declares it.

    A preview stack uses the manifest of the App stack with the same App: ``preview/``
    reads ``10.app/env.manifest.json``.
    """
    directory = (ROOT / compose_path).parent
    manifest = directory / "env.manifest.json"
    if not manifest.is_file() and directory.name == "preview":
        manifest = directory.parent / "10.app" / "env.manifest.json"
    if not manifest.is_file():
        return True
    fields = json.loads(manifest.read_text(encoding="utf-8")).get("fields", [])
    return any(f.get("env") == "SERVICE_VERSION" for f in fields)


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
    expected = SERVICE_VERSION if manifest_declares_service_version(path) else None
    assert {n: v for n, v in services.items() if v != expected} == {}


@pytest.mark.parametrize(
    ("path", "declares"),
    [
        ("platform/30.todo/compose.yaml", True),
        ("finance_report/finance_report/10.app/compose.yaml", False),
        ("finance_report/finance_report/preview/compose.yaml", False),
        ("truealpha/truealpha/20.data_engine/compose.yaml", False),
    ],
)
def test_the_manifest_decides_service_version(path: str, declares: bool) -> None:
    """Todo (SDK 4.8.0) declares it. The pinned Apps (older SDK) do not declare it yet.

    When an App release adopts SDK 4.8.0, its projected manifest declares SERVICE_VERSION,
    this case fails, and the App stack must then set it.
    """
    assert manifest_declares_service_version(path) is declares


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
