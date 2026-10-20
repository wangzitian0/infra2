"""The part of an App env manifest that infra2 acts on (infra2#1320).

infra2 turns an App manifest into three things: a Vault Agent template and policy
(``tools/secrets_render.py``), the Vault values it supplies at deploy time, and the
compose environment for release and decision fields. A ``code`` field is a default inside
the image, so infra2 reads none of it. Two manifests that differ only in ``code`` fields,
or only in the attributes of ``IGNORED_ATTRIBUTES``, give the same deploy.

The receiver compares this contract. It does not compare the bytes of the manifests,
because a byte comparison refuses a release for a change that infra2 never reads.
``common/tests/test_app_contract.py`` proves the split against the SDK renderer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from infra2_sdk.runtime.config_schema import EnvironmentManifest

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
)
#: The attributes that no infra2 code path reads when it renders, supplies or composes.
#: Together with CONTRACT_ATTRIBUTES they name every attribute of the SDK field. A test
#: fails when a new SDK version adds an attribute that is in neither tuple.
IGNORED_ATTRIBUTES: tuple[str, ...] = (
    "field",
    "aliases",
    "has_default",
    "sensitive",
    "injected",
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
        field.env: {name: getattr(field, name) for name in CONTRACT_ATTRIBUTES}
        for field in manifest.fields
        if field.source != "code" or field.rendered
    }


def unmet(release: Contract, provided: Contract) -> list[Gap]:
    """What ``release`` needs and ``provided`` does not supply, in environment-name order.

    ``provided`` may supply more than the release needs: an older release deploys
    on a newer infra2, and a field that a release dropped stays until no release needs it.
    """
    gaps = []
    for env, wanted in sorted(release.items()):
        have = provided.get(env)
        if have is None:
            gaps.append(Gap(env, f"missing (source {wanted['source']})"))
        elif have != wanted:
            changes = "; ".join(
                f"{name}: release {wanted[name]!r}, infra2 {have[name]!r}"
                for name in CONTRACT_ATTRIBUTES
                if wanted[name] != have[name]
            )
            gaps.append(Gap(env, f"differs ({changes})"))
    return gaps


__all__ = [
    "CONTRACT_ATTRIBUTES",
    "Contract",
    "Gap",
    "IGNORED_ATTRIBUTES",
    "parse_contract",
    "unmet",
]
