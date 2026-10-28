"""The App contract the receiver compares and renders (#1320): what it keeps and what it drops.

The receiver renders the Vault Agent template from the deployed release manifest merged
with the stack baseline manifest, and refuses a release only for a non-Vault variable that
the stack compose contract does not supply or a Vault path not granted by ``vault-policy.hcl``.
These tests prove the split against the SDK renderer that makes the Vault template and
policy, and against the SDK start check that stops an image without its variables.
"""

from __future__ import annotations

import json
from dataclasses import fields, replace
from pathlib import Path

import pytest
from infra2_sdk.runtime.config_schema import (
    EnvironmentField,
    EnvironmentManifest,
    validate_environment,
)
from infra2_sdk.secrets import render_agent_policy, render_agent_template

from common.deploy.app_contract import (
    APP_UPSTREAM_MANIFESTS,
    CONTRACT_ATTRIBUTES,
    IGNORED_ATTRIBUTES,
    parse_compose_contract,
    parse_contract,
    render_release_template,
    service_for_id,
    service_stack_manifest,
    service_vault_policy,
    unmet,
)

ROOT = Path(__file__).resolve().parents[2]


def _field(env: str, source: str = "code", **overrides) -> EnvironmentField:
    return EnvironmentField(field=env.lower(), env=env, source=source, **overrides)


def _body(*members: EnvironmentField) -> str:
    return json.dumps(
        EnvironmentManifest(source="apps/x", fields=tuple(members)).to_dict()
    )


#: One field of every kind that the template, the supply, the compose environment or the
#: start check reads, and the code fields that none of them reads.
BASE = (
    _field("HUMAN_KEY", "human", required=True, group="Secrets"),
    _field("HUMAN_OPTIONAL", "human", empty_ok=True, store_key="OPTIONAL_KEY"),
    _field("RUNTIME_TOKEN", "runtime", mirror_to_1password=True, sensitive=True),
    _field("FROM_PROVIDER", "runtime", provided_by="platform/postgres:root_password"),
    _field("COMPOSED_URL", "runtime", composed_from="postgres://{HUMAN_KEY}@db"),
    _field("RELEASE_ID", "release", injected=True),
    _field("DECISION_FLAG", "decision", injected=True),
    _field("SKIPPED_BY_GROUP", "human", group="Skip"),
    _field("CODE_INJECTED", "code", injected=True, aliases=("OLD_CODE_INJECTED",)),
    _field("CODE_REQUIRED", "code", required=True),
    _field("CODE_ONLY", "code", has_default=True, description="a default in the image"),
    _field("CODE_COMPOSED", "code", composed_from="{HUMAN_KEY}-x"),
)
ALTERNATIVE = {
    "field": "renamed_field",
    "has_default": False,
    "sensitive": True,
    "description": "a new description",
    "ci": "allowed",
}


def _rendered(members: tuple[EnvironmentField, ...]) -> tuple[str, str]:
    manifest = EnvironmentManifest(source="apps/x", fields=members)
    template = render_agent_template(
        manifest, project="truealpha", service="app", exclude_groups=("Skip",)
    )
    policy = render_agent_policy(manifest, project="truealpha", service="app")
    return template, policy


def _missing_at_start(members: tuple[EnvironmentField, ...]) -> tuple[str, ...]:
    manifest = EnvironmentManifest(source="apps/x", fields=members)
    return validate_environment(manifest, {}, require_injected=True).missing


def test_every_sdk_field_attribute_is_classified_exactly_once() -> None:
    """A new SDK attribute must be sorted into the contract or the ignored set."""
    sdk = {field.name for field in fields(EnvironmentField)}
    assert sdk == set(CONTRACT_ATTRIBUTES) | set(IGNORED_ATTRIBUTES)
    assert not set(CONTRACT_ATTRIBUTES) & set(IGNORED_ATTRIBUTES)


@pytest.mark.parametrize("attribute", IGNORED_ATTRIBUTES)
def test_an_ignored_attribute_changes_none_of_the_sdk_outputs_nor_the_contract(
    attribute: str,
) -> None:
    changed = tuple(replace(m, **{attribute: ALTERNATIVE[attribute]}) for m in BASE)
    assert changed != BASE
    assert _rendered(changed) == _rendered(BASE)
    assert _missing_at_start(changed) == _missing_at_start(BASE)
    assert parse_contract(_body(*changed), label="x") == parse_contract(
        _body(*BASE), label="x"
    )


def test_a_plain_code_field_changes_none_of_the_sdk_outputs_nor_the_contract() -> None:
    plain = tuple(member for member in BASE if member.env != "CODE_ONLY")
    added = (*BASE, _field("ANOTHER_CODE_DEFAULT", "code", group="Other"))
    edited = tuple(
        replace(m, group="Renamed", sensitive=True, store_key="K", scope="project")
        if m.env == "CODE_ONLY"
        else m
        for m in BASE
    )
    for variant in (plain, added, edited):
        assert _rendered(variant) == _rendered(BASE)
        assert _missing_at_start(variant) == _missing_at_start(BASE)
        assert parse_contract(_body(*variant), label="x") == parse_contract(
            _body(*BASE), label="x"
        )


def test_the_contract_holds_exactly_the_fields_that_infra2_must_supply() -> None:
    contract = parse_contract(_body(*BASE), label="x")
    template, _policy = _rendered(BASE)
    for member in BASE:
        acted = (
            member.source != "code"
            or member.rendered
            or member.required
            or member.injected
        )
        assert (member.env in contract) is acted, member.env
        if member.group != "Skip" and member.rendered:
            assert f"{member.env}=" in template, member.env
        if not acted:
            assert member.env not in template, member.env
    assert {"CODE_COMPOSED", "CODE_INJECTED", "CODE_REQUIRED"} <= set(contract)
    assert "CODE_ONLY" not in contract


def test_parse_compose_contract_keeps_only_non_vault_fields() -> None:
    compose_contract = parse_compose_contract(_body(*BASE), label="x")
    assert set(compose_contract) == {
        "RELEASE_ID",
        "DECISION_FLAG",
        "CODE_INJECTED",
        "CODE_REQUIRED",
    }


def test_every_field_that_the_image_cannot_start_without_is_in_the_contract() -> None:
    contract = parse_contract(_body(*BASE), label="x")
    missing = set(_missing_at_start(BASE))
    assert {"CODE_INJECTED", "CODE_REQUIRED"} <= missing
    assert missing <= set(contract)


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
        ("injected", True),
        ("aliases", ("OLD_NAME",)),
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
    [gap] = unmet(wanted, [supplied])
    assert gap.env == "API_KEY"
    assert attribute in gap.detail


def test_the_order_of_aliases_is_not_a_change() -> None:
    first = parse_contract(_body(_field("A", "human", aliases=("X", "Y"))), label="x")
    second = parse_contract(_body(_field("A", "human", aliases=("Y", "X"))), label="x")
    assert unmet(first, [second]) == []


def test_a_variable_is_gone_not_changed_when_the_env_name_differs() -> None:
    wanted = parse_contract(_body(_field("NEW_NAME", "human")), label="x")
    supplied = parse_contract(_body(_field("OLD_NAME", "human")), label="x")
    assert [gap.env for gap in unmet(wanted, [supplied])] == ["NEW_NAME"]
    assert unmet(supplied, [supplied]) == []


def test_infra2_may_supply_more_than_the_release_needs() -> None:
    wanted = parse_contract(_body(_field("A", "human")), label="x")
    supplied = parse_contract(
        _body(_field("A", "human"), _field("B", "runtime")), label="x"
    )
    assert unmet(wanted, [supplied]) == []
    assert [gap.env for gap in unmet(supplied, [wanted])] == ["B"]


def test_a_variable_that_any_manifest_of_the_stack_supplies_is_met() -> None:
    wanted = parse_contract(
        _body(_field("A", "human"), _field("B", "release", injected=True)), label="x"
    )
    copy = parse_contract(_body(_field("A", "human")), label="copy")
    stack = parse_contract(_body(_field("B", "release", injected=True)), label="stack")
    assert [gap.env for gap in unmet(wanted, [copy])] == ["B"]
    assert unmet(wanted, [copy, stack]) == []
    different = parse_contract(_body(_field("B", "decision", injected=True)), label="s")
    [gap] = unmet(wanted, [copy, different])
    assert gap.env == "B" and "source" in gap.detail


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


def test_a_manifest_of_only_plain_code_fields_is_a_valid_empty_contract() -> None:
    assert parse_contract(_body(_field("A"), _field("B")), label="x") == {}


def test_every_app_upstream_service_has_a_stack_manifest_and_vault_policy() -> None:
    """GREEN-WHILE-EMPTY: every entry in APP_UPSTREAM_MANIFESTS resolves to a real stack."""
    assert set(APP_UPSTREAM_MANIFESTS) == {"finance_report", "truealpha"}
    for app, entries in APP_UPSTREAM_MANIFESTS.items():
        assert entries, app
        for service_id, upstream in entries:
            assert service_id.startswith(f"{app}/")
            assert upstream.endswith(".json")
            stack_path = service_stack_manifest(service_id)
            policy_path = service_vault_policy(service_id)
            assert (ROOT / stack_path).is_file(), stack_path
            assert (ROOT / policy_path).is_file(), policy_path
            contract = parse_contract(
                (ROOT / stack_path).read_text("utf-8"), label=stack_path
            )
            assert contract, stack_path


def test_render_release_template_renders_new_and_tightened_vault_variables() -> None:
    """#1320 Acceptance Criteria 1 & 3: a release manifest that adds a new required Vault
    variable or tightens an existing variable (from required=False, empty_ok=True to
    required=True, empty_ok=False, such as SEC_USER_AGENT in truealpha#1382) renders both
    into the Vault Agent template without a manual sync step in infra2."""
    stack_before_sync = _body(
        _field(
            "DATABASE_URL",
            "runtime",
            provided_by="truealpha/postgres:POSTGRES_PASSWORD",
            composed_from="postgresql://postgres:{POSTGRES_PASSWORD}@127.0.0.1:{env:TA_POSTGRES_PORT}/truealpha",
        ),
        _field(
            "SEC_USER_AGENT",
            "human",
            required=False,
            empty_ok=True,
            scope="project",
        ),
    )
    release_1382 = _body(
        _field(
            "DATABASE_URL",
            "runtime",
            provided_by="truealpha/postgres:POSTGRES_PASSWORD",
            composed_from="postgresql://postgres:{POSTGRES_PASSWORD}@127.0.0.1:{env:TA_POSTGRES_PORT}/truealpha",
        ),
        _field(
            "SEC_USER_AGENT",
            "human",
            required=True,
            injected=True,
            empty_ok=False,
            scope="project",
        ),
        _field(
            "BRAND_NEW_SECRET",
            "human",
            required=True,
            empty_ok=False,
        ),
    )
    template, _policy = render_release_template(
        "truealpha/data_engine",
        [("apps/data-engine/required-env.generated.json", release_1382)],
        stack_manifest_body=stack_before_sync,
    )
    assert 'SEC_USER_AGENT={{ printf "%q" .Data.data.SEC_USER_AGENT }}' in template
    assert 'BRAND_NEW_SECRET={{ printf "%q" .Data.data.BRAND_NEW_SECRET }}' in template
    assert 'with index .Data.data "SEC_USER_AGENT"' not in template
    with pytest.raises(
        ValueError, match="declares invalid environment variable name 'BAD-NAME'"
    ):
        parse_contract(_body(_field("BAD-NAME", "human")), label="bad.json")


def test_render_release_template_refuses_ungranted_vault_policy_path() -> None:
    """Falsifiability: a release manifest requesting a Vault path not in vault-policy.hcl fails."""
    release_with_redis = _body(
        _field(
            "REDIS_URL",
            "runtime",
            provided_by="platform/redis:password",
            composed_from="redis://:{password}@127.0.0.1:6379/0",
        )
    )
    with pytest.raises(
        ValueError,
        match=r"requires Vault path secret/data/platform/\{\{env\}\}/redis",
    ):
        render_release_template(
            "truealpha/data_engine",
            [("apps/data-engine/required-env.generated.json", release_with_redis)],
        )


def test_render_release_template_refuses_conflicting_declarations_and_invalid_inputs() -> (
    None
):
    with pytest.raises(ValueError, match="unknown service"):
        service_for_id("unknown/service")
    with pytest.raises(ValueError, match="no release manifests"):
        render_release_template("truealpha/app", [])
    with pytest.raises(ValueError, match="not a valid Vault policy"):
        render_release_template(
            "truealpha/data_engine",
            [
                (
                    "apps/data-engine/required-env.generated.json",
                    _body(_field("A", "human")),
                )
            ],
            committed_policy="# empty policy",
        )
    first = _body(_field("SHARED_KEY", "human"))
    second = _body(_field("SHARED_KEY", "runtime"))
    with pytest.raises(ValueError, match="SHARED_KEY.source declared differently"):
        render_release_template(
            "truealpha/app",
            [("app-web", first), ("llm-service", second)],
        )
