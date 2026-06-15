"""Infra-009: deploy_v2 contract proof (the 5-axis coordinate).

Proves the SSOT core.environments §4.7 predicates over the existing Infra-009 axes:
the five axes, sub_domain pinning for staging/prod, preview multi-instance addressing
(reusing preview_alias), and the prod_only / env_shared rules.
"""

from __future__ import annotations

import pytest

from tools.deploy_contract import (
    DEPLOY_TYPES,
    SERVICES,
    DeployTarget,
    ServiceSpec,
    deploy_type_spec,
    make_deploy_target,
    make_target,
    service_spec,
    sub_domain_for,
    validate_deploy_target,
)

SHA_A = "a" * 40
SHA_B = "b" * 40

REPORT = SERVICES["finance_report/app"]
# Synthetic specs to exercise the prod_only / env_shared / backing branches that the
# seed registry does not yet contain (those services join when the front door unifies).
SIGNOZ = ServiceSpec("platform/signoz", "signoz", web_facing=True, prod_only=True)
VAULT = ServiceSpec("bootstrap/vault", "vault", web_facing=True, env_shared=True)
REPORT_PG = ServiceSpec("finance_report/postgres", "report-pg", web_facing=False)


def _target(service, env, code=SHA_A, iac=SHA_B, **alias):
    return make_deploy_target(
        service=service, env=env, code_version=code, iac_ref=iac, **alias
    )


# --- sub_domain derivation / pinning --------------------------------------


def test_staging_prod_sub_domain_pinned_by_env():
    assert _target("finance_report/app", "prod").sub_domain == "report"
    assert _target("finance_report/app", "staging").sub_domain == "report-staging"


def test_sub_domain_for_reuses_env_config_and_preview_alias():
    assert sub_domain_for(REPORT, "prod") == "report"
    assert sub_domain_for(REPORT, "staging") == "report-staging"
    assert (
        sub_domain_for(REPORT, "preview", alias_kind="pr", alias_value=5)
        == "report-pr-5"
    )
    assert sub_domain_for(REPORT, "preview", alias_kind="main") == "report-main"
    assert (
        sub_domain_for(REPORT, "preview", alias_kind="commit", alias_value="1ab32d5e")
        == "report-commit-1ab32d5"
    )


def test_env_shared_carries_no_suffix():
    assert sub_domain_for(VAULT, "staging") == "vault"
    assert sub_domain_for(VAULT, "prod") == "vault"


# --- preview multi-instance addressing ------------------------------------


@pytest.mark.parametrize(
    "kind,value,expected",
    [
        ("main", None, "report-main"),
        ("pr", 123, "report-pr-123"),
        ("commit", "1ab32d5e9f", "report-commit-1ab32d5"),
    ],
)
def test_preview_slots(kind, value, expected):
    t = _target("finance_report/app", "preview", alias_kind=kind, alias_value=value)
    assert t.env == "preview" and t.sub_domain == expected


def test_preview_main_and_same_commit_are_distinct_slots():
    a = _target("finance_report/app", "preview", alias_kind="main")
    b = _target(
        "finance_report/app", "preview", alias_kind="commit", alias_value="1ab32d5"
    )
    assert a.sub_domain != b.sub_domain  # the collision the 4-tuple could not express


def test_preview_requires_alias():
    with pytest.raises(ValueError, match="preview requires an alias"):
        _target("finance_report/app", "preview")


def test_preview_rejects_bad_alias_value():
    with pytest.raises(ValueError):
        _target("finance_report/app", "preview", alias_kind="pr", alias_value=0)
    with pytest.raises(ValueError):
        _target("finance_report/app", "preview", alias_kind="commit", alias_value="zz")


# --- sha shapes ------------------------------------------------------------


def test_code_version_and_iac_ref_must_be_sha():
    with pytest.raises(ValueError, match="code_version"):
        _target("finance_report/app", "prod", code="main")
    with pytest.raises(ValueError, match="iac_ref"):
        _target("finance_report/app", "prod", iac="v1.2.3")


def test_shas_are_lowercased():
    t = _target("finance_report/app", "prod", code="A" * 40, iac="B" * 40)
    assert t.code_version == SHA_A and t.iac_ref == SHA_B


# --- service registry ------------------------------------------------------


def test_unknown_service_rejected():
    with pytest.raises(ValueError, match="unknown service"):
        _target("platform/nope", "prod")


def test_service_spec_lookup():
    assert service_spec("finance_report/app").base_subdomain == "report"


# --- prod_only / env_shared (validate_deploy_target directly) --------------


def test_prod_only_rejects_non_prod():
    validate_deploy_target(
        DeployTarget("platform/signoz", "prod", "signoz", SHA_A, SHA_B), SIGNOZ
    )
    with pytest.raises(ValueError, match="prod-only"):
        validate_deploy_target(
            DeployTarget("platform/signoz", "staging", "signoz-staging", SHA_A, SHA_B),
            SIGNOZ,
        )


def test_env_shared_no_preview_and_no_suffix():
    validate_deploy_target(
        DeployTarget("bootstrap/vault", "staging", "vault", SHA_A, SHA_B), VAULT
    )
    with pytest.raises(ValueError, match="no preview instances"):
        validate_deploy_target(
            DeployTarget("bootstrap/vault", "preview", "vault-pr-1", SHA_A, SHA_B),
            VAULT,
        )


@pytest.mark.parametrize("spec", [SIGNOZ, VAULT])
def test_no_preview_instance_error_not_masked_by_missing_alias(spec):
    # A prod_only / env_shared service has no preview slot at all. sub_domain_for must
    # report THAT (the real, unfixable reason) rather than failing first on a missing
    # alias_kind — which a caller could never satisfy anyway.
    with pytest.raises(ValueError, match="no preview instances"):
        sub_domain_for(spec, "preview")  # alias_kind omitted on purpose


def test_backing_service_pins_like_stack_not_routed():
    assert REPORT_PG.web_facing is False
    validate_deploy_target(
        DeployTarget(
            "finance_report/postgres", "staging", "report-pg-staging", SHA_A, SHA_B
        ),
        REPORT_PG,
    )


def test_wrong_subdomain_for_env_rejected():
    with pytest.raises(ValueError, match="requires sub_domain"):
        validate_deploy_target(
            DeployTarget("finance_report/app", "prod", "report-staging", SHA_A, SHA_B),
            REPORT,
        )


# --- serialization ---------------------------------------------------------


# --- deploy type discriminant ---------------------------------------------


def test_unknown_deploy_type_rejected():
    with pytest.raises(ValueError, match="unknown deploy type"):
        deploy_type_spec("prod-but-typo")


def test_type_first_derives_env_and_sub_domain():
    # `type` is the input; env + sub_domain are DERIVED (env is no longer a separate axis).
    t = make_target(
        "staging", service="finance_report/app", version=SHA_A, iac_ref=SHA_B
    )
    assert (t.env, t.sub_domain) == ("staging", "report-staging")
    p = make_target("prod", service="finance_report/app", version=SHA_A, iac_ref=SHA_B)
    assert (p.env, p.sub_domain) == ("prod", "report")


@pytest.mark.parametrize(
    "deploy_type,alias_value,expected",
    [
        ("preview/main", None, "report-main"),
        ("preview/pr", 7, "report-pr-7"),
        ("preview/commit", "1ab32d5", "report-commit-1ab32d5"),
        ("canary", 999, "report-pr-999"),  # canary is an explicit type -> a pr preview
    ],
)
def test_type_first_preview_aliases(deploy_type, alias_value, expected):
    t = make_target(
        deploy_type,
        service="finance_report/app",
        version=SHA_A,
        iac_ref=SHA_B,
        alias_value=alias_value,
    )
    assert t.env == "preview"
    assert t.sub_domain == expected


def test_only_prod_type_requires_review():
    # RL-DATA-1 gate derives from the type, not a loose flag.
    assert deploy_type_spec("prod").requires_review is True
    assert all(not s.requires_review for k, s in DEPLOY_TYPES.items() if k != "prod")


def test_type_is_fail_closed_like_the_axes():
    # an unknown type never reaches a backend (same fail-closed contract as the axes)
    with pytest.raises(ValueError):
        make_target("staging", service="nope/svc", version=SHA_A, iac_ref=SHA_B)


def test_to_dict():
    assert _target("finance_report/app", "prod").to_dict() == {
        "service": "finance_report/app",
        "env": "prod",
        "sub_domain": "report",
        "code_version": SHA_A,
        "iac_ref": SHA_B,
    }
