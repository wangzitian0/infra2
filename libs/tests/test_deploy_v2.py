"""Infra-009: unified deploy front door (deploy_v2) routing + red-line proof.

Tests routing/validation/data-lane only — the backends (preview_lifecycle.up,
deploy_primitive.deploy) are already covered by their own suites and are monkeypatched
here to recorders, so NO Dokploy/HTTP call happens.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

import tools.deploy_v2 as dv2
from tools.deploy_v2 import (
    DeployV2Result,
    deploy_v2,
    enforce_data_lane_red_lines,
    resolve_data_lane,
)

SHA_CODE = "c" * 40
SHA_IAC = "d" * 40


@dataclass
class _PreviewResult:
    alias: str
    compose_id: str
    sha: str
    url: str
    healthy: bool | None


@pytest.fixture
def calls(monkeypatch):
    """Record backend invocations instead of hitting Dokploy."""
    rec = {"preview": None, "fixed": None}

    def fake_up(kind, value, **kw):
        rec["preview"] = {"kind": kind, "value": value, **kw}
        return _PreviewResult(
            alias=f"{kind}" if kind == "main" else f"{kind}-{value}",
            compose_id="cmp-preview",
            sha=kw["code"],
            url=f"https://report-x.{kw['domain']}",
            healthy=True,
        )

    @dataclass
    class _Plan:
        env: str
        sha: str
        compose_id: str
        data: str
        env_vars: dict

    def fake_deploy(env, code, **kw):
        rec["fixed"] = {"env": env, "code": code, **kw}
        return _Plan(env=env, sha=code, compose_id=f"cmp-{env}", data="x", env_vars={})

    monkeypatch.setattr(dv2, "_preview_up", fake_up)
    monkeypatch.setattr(dv2, "_deploy_fixed", fake_deploy)
    return rec


# --- routing ---------------------------------------------------------------


def test_preview_routes_to_lifecycle_with_iac_ref_as_branch(calls):
    res = deploy_v2(
        service="finance_report/app",
        env="preview",
        code_version=SHA_CODE,
        iac_ref=SHA_IAC,
        alias_kind="pr",
        alias_value=7,
        client=object(),
        domain="zitian.party",
    )
    assert isinstance(res, DeployV2Result)
    assert res.backend == "preview-lifecycle"
    assert res.target.sub_domain == "report-pr-7"
    assert calls["preview"]["branch"] == SHA_IAC  # iac_ref pins the source ref
    assert calls["preview"]["code"] == SHA_CODE
    assert calls["fixed"] is None


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_staging_prod_route_to_fixed_primitive(calls, env):
    res = deploy_v2(
        service="finance_report/app",
        env=env,
        code_version=SHA_CODE,
        iac_ref=SHA_IAC,
        client=object(),
        domain="zitian.party",
        staging_validated=True,  # allow prod
    )
    assert res.backend == "deploy-primitive"
    assert calls["fixed"]["env"] == env
    assert calls["fixed"]["code"] == SHA_CODE
    assert res.detail["iac_ref"] == SHA_IAC
    assert calls["preview"] is None


# --- validation fires before any backend ----------------------------------


def test_unknown_service_rejected_before_dispatch(calls):
    with pytest.raises(ValueError, match="unknown service"):
        deploy_v2(
            service="platform/postgres",
            env="prod",
            code_version=SHA_CODE,
            iac_ref=SHA_IAC,
            client=object(),
            domain="zitian.party",
            staging_validated=True,
        )
    assert calls["fixed"] is None and calls["preview"] is None


def test_bad_sha_rejected_before_dispatch(calls):
    with pytest.raises(ValueError, match="code_version"):
        deploy_v2(
            service="finance_report/app",
            env="staging",
            code_version="main",
            iac_ref=SHA_IAC,
            client=object(),
            domain="zitian.party",
        )
    assert calls["fixed"] is None


def test_preview_without_alias_rejected(calls):
    with pytest.raises(ValueError, match="preview requires an alias"):
        deploy_v2(
            service="finance_report/app",
            env="preview",
            code_version=SHA_CODE,
            iac_ref=SHA_IAC,
            client=object(),
            domain="zitian.party",
        )
    assert calls["preview"] is None


# --- data lane / red lines -------------------------------------------------


def test_resolve_data_lane_by_env():
    from tools.deploy_contract import make_deploy_target

    prod = make_deploy_target(
        service="finance_report/app", env="prod", code_version=SHA_CODE, iac_ref=SHA_IAC
    )
    staging = make_deploy_target(
        service="finance_report/app",
        env="staging",
        code_version=SHA_CODE,
        iac_ref=SHA_IAC,
    )
    preview = make_deploy_target(
        service="finance_report/app",
        env="preview",
        code_version=SHA_CODE,
        iac_ref=SHA_IAC,
        alias_kind="main",
    )
    assert resolve_data_lane(prod) == "prod"
    assert resolve_data_lane(staging) == "staging"
    assert resolve_data_lane(preview) == "staging"  # preview defaults to staging data


def test_red_line_unreviewed_code_not_on_prod_data(calls):
    with pytest.raises(ValueError, match="RL-DATA-1"):
        deploy_v2(
            service="finance_report/app",
            env="prod",
            code_version=SHA_CODE,
            iac_ref=SHA_IAC,
            client=object(),
            domain="zitian.party",
            staging_validated=True,
            code_reviewed=False,
        )
    assert calls["fixed"] is None


def test_enforce_returns_data_lane():
    from tools.deploy_contract import make_deploy_target

    t = make_deploy_target(
        service="finance_report/app", env="prod", code_version=SHA_CODE, iac_ref=SHA_IAC
    )
    assert enforce_data_lane_red_lines(t, code_reviewed=True) == "prod"
