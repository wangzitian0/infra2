"""The App contract the receiver compares (#1320): what it keeps and what it drops.

The receiver refuses a release only for a variable that infra2 must supply. These tests
prove the split against the SDK renderer that makes the Vault template and policy: a
change the contract drops never changes the rendered output, and each attribute the
contract keeps changes the comparison.
"""

from __future__ import annotations

import json
from dataclasses import fields, replace
from pathlib import Path

import pytest
from infra2_sdk.runtime.config_schema import EnvironmentField, EnvironmentManifest
from infra2_sdk.secrets import render_agent_policy, render_agent_template

from common.deploy.app_contract import (
    CONTRACT_ATTRIBUTES,
    IGNORED_ATTRIBUTES,
    parse_contract,
    unmet,
)

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "contracts" / "apps.lock.json"


def _field(env: str, source: str = "code", **overrides) -> EnvironmentField:
    return EnvironmentField(field=env.lower(), env=env, source=source, **overrides)


def _body(*members: EnvironmentField) -> str:
    return json.dumps(
        EnvironmentManifest(source="apps/x", fields=tuple(members)).to_dict()
    )


#: One field of every kind that the template, the supply or the compose environment reads,
#: and the code fields that none of them reads.
BASE = (
    _field("HUMAN_KEY", "human", required=True, group="Secrets"),
    _field("HUMAN_OPTIONAL", "human", empty_ok=True, store_key="OPTIONAL_KEY"),
    _field("RUNTIME_TOKEN", "runtime", mirror_to_1password=True, sensitive=True),
    _field("FROM_PROVIDER", "runtime", provided_by="platform/postgres:root_password"),
    _field("COMPOSED_URL", "runtime", composed_from="postgres://{HUMAN_KEY}@db"),
    _field("RELEASE_ID", "release", injected=True),
    _field("DECISION_FLAG", "decision", injected=True),
    _field("SKIPPED_BY_GROUP", "human", group="Skip"),
    _field("CODE_ONLY", "code", has_default=True, description="a default in the image"),
    _field("CODE_COMPOSED", "code", composed_from="{HUMAN_KEY}-x"),
)
ALTERNATIVE = {
    "field": "renamed_field",
    "aliases": (),
    "has_default": False,
    "sensitive": True,
    "injected": True,
    "description": "a new description",
    "ci": "allowed",
}


def _alternative(member: EnvironmentField, attribute: str) -> object:
    """A valid new value of ``attribute`` for ``member``. An alias must be unique per estate."""
    if attribute == "aliases":
        return (f"ALIAS_OF_{member.env}",)
    return ALTERNATIVE[attribute]


def _rendered(members: tuple[EnvironmentField, ...]) -> tuple[str, str]:
    manifest = EnvironmentManifest(source="apps/x", fields=members)
    template = render_agent_template(
        manifest, project="truealpha", service="app", exclude_groups=("Skip",)
    )
    policy = render_agent_policy(manifest, project="truealpha", service="app")
    return template, policy


def test_every_sdk_field_attribute_is_classified_exactly_once() -> None:
    """A new SDK attribute must be sorted into the contract or the ignored set."""
    sdk = {field.name for field in fields(EnvironmentField)}
    assert sdk == set(CONTRACT_ATTRIBUTES) | set(IGNORED_ATTRIBUTES)
    assert not set(CONTRACT_ATTRIBUTES) & set(IGNORED_ATTRIBUTES)


@pytest.mark.parametrize("attribute", IGNORED_ATTRIBUTES)
def test_an_ignored_attribute_changes_neither_the_template_nor_the_contract(
    attribute: str,
) -> None:
    changed = tuple(
        replace(member, **{attribute: _alternative(member, attribute)})
        for member in BASE
    )
    assert changed != BASE
    assert _rendered(changed) == _rendered(BASE)
    assert parse_contract(_body(*changed), label="x") == parse_contract(
        _body(*BASE), label="x"
    )


def test_a_code_field_changes_neither_the_template_nor_the_contract() -> None:
    plain = tuple(member for member in BASE if member.env != "CODE_ONLY")
    added = (*BASE, _field("ANOTHER_CODE_DEFAULT", "code", group="Other"))
    edited = tuple(
        replace(m, group="Renamed", required=True, sensitive=True, store_key="K")
        if m.env == "CODE_ONLY"
        else m
        for m in BASE
    )
    for variant in (plain, added, edited):
        assert _rendered(variant) == _rendered(BASE)
        assert parse_contract(_body(*variant), label="x") == parse_contract(
            _body(*BASE), label="x"
        )


def test_the_contract_holds_exactly_the_fields_that_the_template_or_the_supply_reads() -> (
    None
):
    contract = parse_contract(_body(*BASE), label="x")
    template, _policy = _rendered(BASE)
    for member in BASE:
        acted = member.source != "code" or member.rendered
        assert (member.env in contract) is acted, member.env
        if member.group != "Skip" and member.rendered:
            assert f"{member.env}=" in template, member.env
        if not acted:
            assert member.env not in template, member.env
    # The code field that composes a value renders, so it is part of the contract.
    assert "CODE_COMPOSED" in contract and "CODE_ONLY" not in contract


@pytest.mark.parametrize(
    "attribute, other",
    [
        ("source", "runtime"),
        ("scope", "project"),
        ("provided_by", "platform/redis:password"),
        ("composed_from", "{OTHER}-x"),
        ("store_key", "OTHER_KEY"),
        ("empty_ok", True),
        ("required", True),
        ("mirror_to_1password", True),
        ("group", "Elsewhere"),
    ],
)
def test_each_contract_attribute_changes_the_comparison(
    attribute: str, other: object
) -> None:
    base = _field(
        "API_KEY", "runtime" if attribute == "mirror_to_1password" else "human"
    )
    changed = replace(base, **{attribute: other})
    wanted = parse_contract(_body(changed), label="x")
    supplied = parse_contract(_body(base), label="x")
    [gap] = unmet(wanted, supplied)
    assert gap.env == "API_KEY"
    assert attribute in gap.detail


def test_a_variable_is_gone_not_changed_when_the_env_name_differs() -> None:
    wanted = parse_contract(_body(_field("NEW_NAME", "human")), label="x")
    supplied = parse_contract(_body(_field("OLD_NAME", "human")), label="x")
    assert [gap.env for gap in unmet(wanted, supplied)] == ["NEW_NAME"]
    assert unmet(supplied, supplied) == []


def test_infra2_may_supply_more_than_the_release_needs() -> None:
    wanted = parse_contract(_body(_field("A", "human")), label="x")
    supplied = parse_contract(
        _body(_field("A", "human"), _field("B", "runtime")), label="x"
    )
    assert unmet(wanted, supplied) == []
    assert [gap.env for gap in unmet(supplied, wanted)] == ["B"]


@pytest.mark.parametrize(
    "body",
    ["", "null", "[]", "{}", '{"vars": ["A"]}', _body()],
    ids=["empty", "null", "list", "object", "legacy-vars", "no-fields"],
)
def test_a_body_that_is_not_a_manifest_is_an_error_not_an_empty_contract(
    body: str,
) -> None:
    with pytest.raises(ValueError, match="the-file is not an App env manifest"):
        parse_contract(body, label="the-file")


def test_a_manifest_of_only_code_fields_is_a_valid_empty_contract() -> None:
    assert parse_contract(_body(_field("A"), _field("B")), label="x") == {}


def test_the_vendored_copies_parse_to_a_non_empty_contract_each() -> None:
    """GREEN-WHILE-EMPTY: a copy that parsed to nothing would pass every release."""
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    assert lock["files"]
    for entry in lock["files"]:
        contract = parse_contract(
            (ROOT / entry["path"]).read_text("utf-8"), label=entry["path"]
        )
        assert contract, entry["path"]
        assert unmet(contract, contract) == []
