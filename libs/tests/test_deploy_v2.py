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


def test_preview_routes_to_lifecycle_cloning_a_branch_ref(calls):
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
    # Dokploy clones a branch/tag ref, not the iac_ref sha (#342): default is infra2 main,
    # while iac_ref stays the recorded identity on the target.
    assert calls["preview"]["branch"] == "main"
    assert res.target.iac_ref == SHA_IAC
    assert calls["preview"]["code"] == SHA_CODE
    assert calls["fixed"] is None


def test_preview_iac_branch_override(calls):
    deploy_v2(
        service="finance_report/app",
        env="preview",
        code_version=SHA_CODE,
        iac_ref=SHA_IAC,
        alias_kind="pr",
        alias_value=7,
        client=object(),
        domain="zitian.party",
        iac_branch="release/1.2",
    )
    assert calls["preview"]["branch"] == "release/1.2"  # clone a specific infra2 ref


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
        code_reviewed=True,  # prod data needs the explicit RL-DATA-1 signal
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


@pytest.mark.parametrize("code_reviewed", [False, None])
def test_red_line_prod_data_fails_closed_without_positive_review(calls, code_reviewed):
    # RL-DATA-1 is deny-by-default: an explicit False AND an omitted/None signal both
    # fail closed, so prod data is unreachable unless review is positively asserted.
    kwargs = dict(
        service="finance_report/app",
        env="prod",
        code_version=SHA_CODE,
        iac_ref=SHA_IAC,
        client=object(),
        domain="zitian.party",
        staging_validated=True,
    )
    if code_reviewed is not None:
        kwargs["code_reviewed"] = code_reviewed
    with pytest.raises(ValueError, match="RL-DATA-1"):
        deploy_v2(**kwargs)
    assert calls["fixed"] is None


def test_prod_data_allowed_with_positive_review(calls):
    res = deploy_v2(
        service="finance_report/app",
        env="prod",
        code_version=SHA_CODE,
        iac_ref=SHA_IAC,
        client=object(),
        domain="zitian.party",
        staging_validated=True,
        code_reviewed=True,
    )
    assert res.backend == "deploy-primitive" and calls["fixed"]["env"] == "prod"


def test_enforce_returns_data_lane():
    from tools.deploy_contract import make_deploy_target

    t = make_deploy_target(
        service="finance_report/app", env="prod", code_version=SHA_CODE, iac_ref=SHA_IAC
    )
    assert enforce_data_lane_red_lines(t, code_reviewed=True) == "prod"


# --- CLI entry (the cutover seam) ------------------------------------------


@pytest.fixture
def cli(monkeypatch):
    """Drive deploy_v2.main with refs/client/backend faked — no resolve, no Dokploy."""
    import json

    from tools.deploy_contract import make_deploy_target

    rec = {}
    monkeypatch.setattr(dv2, "_resolve_refs", lambda code, iac: (SHA_CODE, SHA_IAC))

    import libs.dokploy as dk

    monkeypatch.setattr(dk, "get_dokploy", lambda host: f"client@{host}")

    def fake_deploy_v2(**kw):
        rec.update(kw)
        target = make_deploy_target(
            service=kw["service"],
            env=kw["env"],
            code_version=kw["code_version"],
            iac_ref=kw["iac_ref"],
            alias_kind=kw.get("alias_kind"),
            alias_value=kw.get("alias_value"),
        )
        return DeployV2Result(target, "staging", "deploy-primitive", {"sha": SHA_CODE})

    monkeypatch.setattr(dv2, "deploy_v2", fake_deploy_v2)
    return rec, json


def test_cli_resolves_and_dispatches(cli, capsys):
    rec, json = cli
    rc = dv2.main(
        ["--env", "staging", "--code", "main", "--iac-ref", "main", "--domain", "zp.io"]
    )
    assert rc == 0
    assert rec["env"] == "staging"
    assert rec["code_version"] == SHA_CODE and rec["iac_ref"] == SHA_IAC
    assert rec["client"] == "client@cloud.zp.io"  # host = cloud.<domain>
    out = json.loads(capsys.readouterr().out)
    assert out["env"] == "staging" and out["backend"] == "deploy-primitive"


def test_cli_code_reviewed_flag_maps_to_true_else_none(cli):
    rec, _ = cli
    dv2.main(["--env", "staging", "--code", "m", "--iac-ref", "m", "--domain", "zp.io"])
    assert rec["code_reviewed"] is None  # omitted stays deny-by-default
    dv2.main(
        [
            "--env",
            "prod",
            "--code",
            "m",
            "--iac-ref",
            "m",
            "--domain",
            "zp.io",
            "--staging-validated",
            "--code-reviewed",
        ]
    )
    assert rec["code_reviewed"] is True  # explicit positive signal


def test_cli_fails_fast_on_unresolvable_ref(monkeypatch, capsys):
    def boom(code, iac):
        raise ValueError("not a full 40-hex commit sha")

    monkeypatch.setattr(dv2, "_resolve_refs", boom)
    rc = dv2.main(
        [
            "--env",
            "staging",
            "--code",
            "deadbeef",
            "--iac-ref",
            "m",
            "--domain",
            "zp.io",
        ]
    )
    assert rc == 2
    assert "ref resolution failed" in capsys.readouterr().err
