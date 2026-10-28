"""The part of an App env manifest that infra2 acts on (infra2#1320).

infra2 turns an App manifest into three things: a Vault Agent template and policy rendered
from the deployed release manifest merged with the stack manifest, the Vault values it
supplies at deploy time, and the compose environment (release and decision fields, and the
hand-written compose file). A field is in the contract when infra2 must supply it: its
``source`` is not ``code``, the template renders it, or the image refuses to boot without
it (``required``, or ``injected`` under ``assert_environment(require_injected=True)``). A
``code`` field that none of these holds is a default inside the image, and infra2 reads
none of it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from infra2_sdk.runtime.config_schema import EnvironmentField, EnvironmentManifest
from infra2_sdk.secrets import render_agent_policy, render_agent_template

from common.core.constants import REPO_ROOT
from common.security.registry import _RENDER_KEYS, SERVICES, Service

ROOT = REPO_ROOT

#: The attributes of a field that change what infra2 renders, supplies or composes.
CONTRACT_ATTRIBUTES: tuple[str, ...] = (
    "env",
    "source",
    "scope",
    "provided_by",
    "composed_from",
    "store_key",
    "empty_ok",
    "required",
    "mirror_to_1password",
    "group",
    "injected",
    "aliases",
)
#: The attributes that no infra2 code path reads when it renders, supplies or composes.
#: Together with CONTRACT_ATTRIBUTES they name every attribute of the SDK field. A test
#: fails when a new SDK version adds an attribute that is in neither tuple.
IGNORED_ATTRIBUTES: tuple[str, ...] = (
    "field",
    "has_default",
    "sensitive",
    "description",
    "ci",
)

#: Upstream release manifest paths in each App repository, paired with the infra2 service id.
APP_UPSTREAM_MANIFESTS: dict[str, tuple[tuple[str, str], ...]] = {
    "finance_report": (
        ("finance_report/app", "common/runtime/required-env.generated.json"),
    ),
    "truealpha": (
        ("truealpha/app", "apps/app-web/required-env.manifest.json"),
        ("truealpha/app", "apps/llm-service/required-env.generated.json"),
        ("truealpha/data_engine", "apps/data-engine/required-env.generated.json"),
    ),
}

#: Environment name -> the contract attributes of the field.
Contract = dict[str, dict[str, object]]

_POLICY_PATH_RE = re.compile(r'^path\s+"([^"]+)"', re.MULTILINE)
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class Gap:
    """One environment variable that a release needs and infra2 does not supply."""

    env: str
    detail: str

    def __str__(self) -> str:
        return f"{self.env}: {self.detail}"


def parse_manifest(body: str | bytes, *, label: str) -> EnvironmentManifest:
    """Parse and validate one App environment manifest.

    Raises ``ValueError`` when ``body`` is not valid manifest JSON or declares no fields.
    """
    try:
        manifest = EnvironmentManifest.from_dict(json.loads(body))
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError(f"{label} is not an App env manifest: {exc!r}") from None
    if not manifest.fields:
        raise ValueError(f"{label} is not an App env manifest: it declares no field")
    for field in manifest.fields:
        if not _ENV_NAME_RE.fullmatch(field.env):
            raise ValueError(
                f"{label} declares invalid environment variable name {field.env!r}"
            )
    return manifest


def parse_contract(body: str | bytes, *, label: str) -> Contract:
    """The contract of one manifest. ``label`` names the file in an error.

    A body that is not a manifest, or declares no field, raises ``ValueError``. An empty
    contract from a truncated file would otherwise ask for nothing and pass.
    """
    manifest = parse_manifest(body, label=label)
    return {
        field.env: {name: _attribute(field, name) for name in CONTRACT_ATTRIBUTES}
        for field in manifest.fields
        if _supplied_by_infra2(field)
    }


def parse_compose_contract(body: str | bytes, *, label: str) -> Contract:
    """The non-Vault compose contract of one manifest.

    Vault-rendered fields (``source`` in ``("human", "runtime")`` or ``composed_from``) are
    rendered into the Vault Agent template from the release manifest (#1320). Fields that the
    Vault template does not render (``release``, ``decision``, or ``required``/``injected``
    ``code`` fields) must be supplied by the stack compose file and deployer at ``iac_ref``.
    """
    manifest = parse_manifest(body, label=label)
    return {
        field.env: {name: _attribute(field, name) for name in CONTRACT_ATTRIBUTES}
        for field in manifest.fields
        if _supplied_by_infra2(field) and not field.rendered
    }


def _supplied_by_infra2(field: EnvironmentField) -> bool:
    return field.source != "code" or field.rendered or field.required or field.injected


def _attribute(field: EnvironmentField, name: str) -> object:
    value = getattr(field, name)
    return tuple(sorted(value)) if name == "aliases" else value


def unmet(release: Contract, provided: Sequence[Contract]) -> list[Gap]:
    """What ``release`` needs and none of the ``provided`` contracts supplies.

    ``provided`` holds one contract per manifest of the stack. A variable is met when one of
    them declares it with the same attributes. ``provided`` may supply more than the release
    needs: an older release deploys on a newer infra2, and a field that a release dropped
    stays until no release needs it.
    """
    gaps = []
    for env, wanted in sorted(release.items()):
        declared = [contract[env] for contract in provided if env in contract]
        if wanted in declared:
            continue
        if not declared:
            gaps.append(Gap(env, f"missing (source {wanted['source']})"))
            continue
        have = declared[0]
        changes = "; ".join(
            f"{name}: release {wanted[name]!r}, infra2 {have[name]!r}"
            for name in CONTRACT_ATTRIBUTES
            if wanted[name] != have[name]
        )
        gaps.append(Gap(env, f"differs ({changes})"))
    return gaps


def service_for_id(service_id: str, *, preview: bool = False) -> Service:
    """Return the registered ``Service`` for ``service_id`` and ``preview`` mode."""
    for service in SERVICES:
        if service.id == service_id and service.preview == preview:
            return service
    raise ValueError(f"unknown service {service_id!r} (preview={preview})")


def service_stack_manifest(service_id: str, *, preview: bool = False) -> str:
    """Return the stack manifest path for ``service_id``."""
    return service_for_id(service_id, preview=preview).manifests[0]


def service_vault_policy(service_id: str, *, preview: bool = False) -> str:
    """Return the committed Vault policy path for ``service_id``."""
    service = service_for_id(service_id, preview=preview)
    return f"{service.directory}/vault-policy.hcl"


def policy_paths(policy_text: str) -> set[str]:
    """Return the set of ``path "..."`` targets declared in a Vault HCL policy."""
    return set(_POLICY_PATH_RE.findall(policy_text))


def render_release_template(
    service_id: str,
    release_bodies: Sequence[tuple[str, str | bytes]],
    *,
    stack_manifest_body: str | bytes | None = None,
    committed_policy: str | None = None,
    preview: bool = False,
    root: Path = ROOT,
) -> tuple[str, str]:
    """Render the Vault Agent template and policy for ``service_id`` from release manifests.

    Merges the upstream ``release_bodies`` (``(label, body)`` pairs) with the stack baseline
    manifest so Vault-backed fields added or tightened in the release render directly into
    ``secrets.ctmpl`` (#1320). Verifies that every Vault path in the rendered policy is
    granted by the stack's committed ``vault-policy.hcl``.
    """
    service = service_for_id(service_id, preview=preview)
    if not release_bodies:
        raise ValueError(f"no release manifests provided for {service_id}")

    release_fields: dict[str, EnvironmentField] = {}
    for label, body in release_bodies:
        manifest = parse_manifest(body, label=label)
        for entry in manifest.fields:
            if entry.env in service.exclude_envs:
                continue
            existing = release_fields.get(entry.env)
            if existing is not None:
                for key in _RENDER_KEYS:
                    if getattr(existing, key) != getattr(entry, key):
                        raise ValueError(
                            f"{service.directory}: {entry.env}.{key} declared differently in {label}"
                        )
                release_fields[entry.env] = replace(
                    existing,
                    required=existing.required or entry.required,
                    injected=existing.injected or entry.injected,
                )
                continue
            release_fields[entry.env] = entry

    stack_path = service.manifests[0]
    raw_stack = (
        stack_manifest_body
        if stack_manifest_body is not None
        else (root / stack_path).read_text(encoding="utf-8")
    )
    stack_manifest = parse_manifest(raw_stack, label=stack_path)

    merged_fields: dict[str, EnvironmentField] = {}
    for entry in stack_manifest.fields:
        if entry.env in service.exclude_envs:
            continue
        merged_fields[entry.env] = release_fields.get(entry.env, entry)
    for env, entry in release_fields.items():
        if env not in merged_fields:
            merged_fields[env] = entry

    merged = EnvironmentManifest(
        source=service.id,
        fields=tuple(merged_fields.values()),
    )
    template = render_agent_template(
        merged,
        project=service.project,
        service=service.service,
        source_env=service.source_env,
        exclude_groups=service.exclude_groups,
    )
    policy = render_agent_policy(
        merged,
        project=service.project,
        service=service.service,
        source_env=service.source_env,
    )

    policy_file = f"{service.directory}/vault-policy.hcl"
    raw_policy = (
        committed_policy
        if committed_policy is not None
        else (root / policy_file).read_text(encoding="utf-8")
    )
    granted = policy_paths(raw_policy)
    if not granted:
        raise ValueError(f"{policy_file} is not a valid Vault policy")
    required = policy_paths(policy)
    ungranted = sorted(required - granted)
    if ungranted:
        raise ValueError(
            f"release manifest for {service_id} requires Vault path "
            f"{', '.join(ungranted)} not granted by {policy_file}"
        )
    return template, policy


__all__ = [
    "APP_UPSTREAM_MANIFESTS",
    "CONTRACT_ATTRIBUTES",
    "Contract",
    "Gap",
    "IGNORED_ATTRIBUTES",
    "parse_compose_contract",
    "parse_contract",
    "parse_manifest",
    "policy_paths",
    "render_release_template",
    "service_for_id",
    "service_stack_manifest",
    "service_vault_policy",
    "unmet",
]
