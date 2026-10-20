"""The part of an App env manifest that infra2 acts on (infra2#1320).

infra2 turns an App manifest into three things: a Vault Agent template and policy
(``tools/secrets_render.py``), the Vault values it supplies at deploy time, and the
compose environment (release and decision fields, and the hand-written compose file).
A field is in the contract when infra2 must supply it: its ``source`` is not ``code``,
the template renders it, or the image refuses to boot without it (``required``, or
``injected`` under ``assert_environment(require_injected=True)``). A ``code`` field
that none of these holds is a default inside the image, and infra2 reads none of it.

The receiver compares this contract. It does not compare the bytes of the manifests,
because a byte comparison refuses a release for a change that infra2 never reads.
``common/tests/test_app_contract.py`` proves the split against the SDK renderer and
the SDK start check.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from infra2_sdk.runtime.config_schema import EnvironmentField, EnvironmentManifest

from common.security.registry import SERVICES

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

#: Environment name -> the contract attributes of the field.
Contract = dict[str, dict[str, object]]


@dataclass(frozen=True)
class Gap:
    """One environment variable that a release needs and infra2 does not supply."""

    env: str
    detail: str

    def __str__(self) -> str:
        return f"{self.env}: {self.detail}"


def parse_contract(body: str | bytes, *, label: str) -> Contract:
    """The contract of one manifest. ``label`` names the file in an error.

    A body that is not a manifest, or declares no field, raises ``ValueError``. An empty
    contract from a truncated file would otherwise ask for nothing and pass.
    """
    try:
        manifest = EnvironmentManifest.from_dict(json.loads(body))
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError(f"{label} is not an App env manifest: {exc!r}") from None
    if not manifest.fields:
        raise ValueError(f"{label} is not an App env manifest: it declares no field")
    return {
        field.env: {name: _attribute(field, name) for name in CONTRACT_ATTRIBUTES}
        for field in manifest.fields
        if _supplied_by_infra2(field)
    }


def _supplied_by_infra2(field: EnvironmentField) -> bool:
    return field.source != "code" or field.rendered or field.required or field.injected


def _attribute(field: EnvironmentField, name: str) -> object:
    value = getattr(field, name)
    return tuple(sorted(value)) if name == "aliases" else value


def unmet(release: Contract, provided: Sequence[Contract]) -> list[Gap]:
    """What ``release`` needs and none of the ``provided`` contracts supplies.

    ``provided`` holds one contract per manifest of the stack: the vendored copy and the
    stack-only manifests of infra2. A variable is met when one of them declares it with
    the same attributes. ``provided`` may supply more than the release needs: an older
    release deploys on a newer infra2, and a field that a release dropped stays until no
    release needs it.
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


def stack_manifests(copy_path: str) -> tuple[str, ...]:
    """The other manifests of every infra2 stack that consumes the vendored ``copy_path``.

    infra2 supplies a variable from the copy or from a stack-only manifest (for example
    the release fields of the data engine, ``env.manifest.json``). The registry lists both.
    """
    paths: dict[str, None] = {}
    for service in SERVICES:
        if copy_path in service.manifests:
            paths.update(
                (path, None) for path in service.manifests if path != copy_path
            )
    return tuple(paths)


__all__ = [
    "CONTRACT_ATTRIBUTES",
    "Contract",
    "Gap",
    "IGNORED_ATTRIBUTES",
    "parse_contract",
    "stack_manifests",
    "unmet",
]
