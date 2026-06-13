"""Tests for the deploy primitive deploy(env, code, data) (finance_report#883 P2 step 3)."""

from __future__ import annotations

import pytest

from tools import deploy_primitive as dp


class FakeDokploy:
    """Records the compose env-update + deploy calls instead of hitting Dokploy."""

    def __init__(self):
        self.updated: list[tuple[str, dict]] = []
        self.deployed: list[str] = []

    def update_compose_env(self, compose_id, env_vars=None, env_str=None):
        self.updated.append((compose_id, dict(env_vars or {})))
        return {}

    def deploy_compose(self, compose_id):
        self.deployed.append(compose_id)
        return {}


def test_staging_deploy_assembles_axes_and_triggers():
    client = FakeDokploy()
    plan = dp.deploy("staging", "deadbeef", domain="zitian.party", client=client)

    assert plan.sha == "deadbeef"
    assert plan.compose_id == "A6V-hbJlgHMwgPDoTDnhH"
    assert plan.data == "staging"
    # the digest + URL + suffix are pushed; update_compose_env merges the rest
    cid, env = client.updated[0]
    assert cid == "A6V-hbJlgHMwgPDoTDnhH"
    assert env["IMAGE_TAG"] == "deadbeef"
    assert env["GIT_COMMIT_SHA"] == "deadbeef"
    assert env["NEXT_PUBLIC_APP_URL"] == "https://report-staging.zitian.party"
    assert env["ENV_SUFFIX"] == "-staging"
    assert client.deployed == ["A6V-hbJlgHMwgPDoTDnhH"]


def test_prod_refuses_unvalidated_digest_promote_not_rebuild():
    client = FakeDokploy()
    with pytest.raises(ValueError, match="requires a staging deploy"):
        dp.deploy("prod", "deadbeef", domain="zitian.party", client=client)
    # the gate prevents ANY side effect: neither env mutation nor deploy
    assert client.updated == []
    assert client.deployed == []


def test_prod_deploys_a_staging_validated_digest():
    client = FakeDokploy()
    plan = dp.deploy(
        "prod", "deadbeef", domain="zitian.party", client=client, staging_validated=True
    )
    assert plan.compose_id == "lNn9gVS1Zyw79Jzw5dlbu"
    assert plan.data == "prod"
    assert client.updated[0][1]["NEXT_PUBLIC_APP_URL"] == "https://report.zitian.party"
    assert client.deployed == ["lNn9gVS1Zyw79Jzw5dlbu"]


def test_prod_break_glass_overrides_the_staging_gate():
    client = FakeDokploy()
    plan = dp.deploy(
        "prod", "deadbeef", domain="zitian.party", client=client, break_glass=True
    )
    assert plan.env == "prod"
    assert client.deployed == ["lNn9gVS1Zyw79Jzw5dlbu"]


def test_preview_is_rejected_as_dynamic():
    client = FakeDokploy()
    with pytest.raises(ValueError, match="per-PR dynamic env"):
        dp.deploy("preview", "deadbeef", domain="zitian.party", client=client)
    # the gate prevents ANY side effect: neither env mutation nor deploy
    assert client.updated == []
    assert client.deployed == []


def test_data_override_is_passed_through():
    client = FakeDokploy()
    plan = dp.deploy(
        "staging", "deadbeef", domain="zitian.party", client=client, data="empty"
    )
    assert plan.data == "empty"


def test_sha_is_lowercased_via_the_resolver():
    client = FakeDokploy()
    plan = dp.deploy("staging", "DEADBEEF", domain="zitian.party", client=client)
    assert plan.sha == "deadbeef"


def test_domain_with_newline_is_rejected_before_any_side_effect():
    # a newline in domain would corrupt the line-based compose env / inject a var
    client = FakeDokploy()
    with pytest.raises(ValueError, match="invalid domain"):
        dp.deploy("staging", "deadbeef", domain="zitian.party\nINJECT=1", client=client)
    assert client.updated == []
    assert client.deployed == []


def test_empty_data_override_is_honored_not_silently_defaulted():
    # data="" is an explicit caller value, not a fallback trigger (is-None check)
    client = FakeDokploy()
    plan = dp.deploy(
        "staging", "deadbeef", domain="zitian.party", client=client, data=""
    )
    assert plan.data == ""
