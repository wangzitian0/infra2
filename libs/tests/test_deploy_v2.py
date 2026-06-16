"""Infra-009: unified deploy front door (deploy_v2) — the (type × version_ref) matrix.

Tests routing / form-gating / image_ref threading / gates / data-lane red lines. The
network resolvers (resolve_image_ref / resolve_pr / resolve_to_sha) and the backends
(preview_lifecycle.up, deploy_primitive.deploy) are monkeypatched, so NO git/Dokploy/HTTP
call happens — classify_ref stays real, so form validation is exercised for real.
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
from tools.resolve_deploy_ref import ResolvedRef, classify_ref

SHA_CODE = "c" * 40
SHA_IAC = "d" * 40


def _fake_resolve_image_ref(ref, **_kw):
    """Resolve like the real one (form via real classify_ref) but with no network.

    A release pulls its tag (image_ref == the tag); code pulls the short sha. A bare sha
    passes through as-is (mirrors resolve_to_sha — short shas are NOT expanded), so the
    front door's full-sha guard (CF4) is exercised for real.
    """
    form = classify_ref(ref)
    if form == "tag":
        return ResolvedRef(sha=SHA_CODE, image_ref=ref.strip(), form=form)
    if form == "release-branch":
        return ResolvedRef(sha=SHA_CODE, image_ref="v9.9.9", form=form)
    if form == "sha":
        s = ref.strip().lower()
        return ResolvedRef(sha=s, image_ref=s[:7], form=form)
    return ResolvedRef(sha=SHA_CODE, image_ref=SHA_CODE[:7], form=form)


def _fake_resolve_pr(pr, **_kw):
    if not (str(pr).strip().isdigit() and int(pr) > 0):
        raise ValueError(f"PR number must be a positive integer, got {pr!r}")
    return ResolvedRef(sha=SHA_CODE, image_ref=SHA_CODE[:7], form="pr")


@dataclass
class _PreviewResult:
    alias: str
    compose_id: str
    sha: str
    url: str
    healthy: bool | None


@dataclass
class _Plan:
    env: str
    sha: str
    compose_id: str
    data: str
    env_vars: dict


@pytest.fixture
def calls(monkeypatch):
    """Record backend invocations + stub the resolvers — no git, no Dokploy."""
    rec = {"preview": None, "fixed": None}

    def fake_up(kind, value, **kw):
        rec["preview"] = {"kind": kind, "value": value, **kw}
        alias = "main" if kind == "main" else f"{kind}-{value}"
        return _PreviewResult(
            alias=alias,
            compose_id="cmp-preview",
            sha=kw["code"],
            url=f"https://report-x.{kw['domain']}",
            healthy=True,
        )

    def fake_deploy(env, code, **kw):
        rec["fixed"] = {"env": env, "code": code, **kw}
        return _Plan(env=env, sha=code, compose_id=f"cmp-{env}", data="x", env_vars={})

    monkeypatch.setattr(dv2, "_preview_up", fake_up)
    monkeypatch.setattr(dv2, "_deploy_fixed", fake_deploy)
    monkeypatch.setattr(dv2, "resolve_image_ref", _fake_resolve_image_ref)
    monkeypatch.setattr(dv2, "resolve_pr", _fake_resolve_pr)
    monkeypatch.setattr(dv2, "resolve_to_sha", lambda ref, **kw: SHA_IAC)
    return rec


def _deploy(**over):
    base = dict(
        service="finance_report/app",
        iac_ref="main",
        client=object(),
        domain="zitian.party",
    )
    base.update(over)
    return deploy_v2(**base)


# --- prod: release-only, pulls the TAG (the headline deliverable) ----------


def test_prod_tag_routes_fixed_and_pulls_the_tag_not_the_sha(calls):
    res = _deploy(
        deploy_type="prod",
        version_ref="v1.2.3",
        staging_validated=True,
        code_reviewed=True,
    )
    assert res.backend == "deploy-primitive"
    assert calls["fixed"]["env"] == "prod"
    assert calls["fixed"]["code"] == SHA_CODE  # identity is the commit
    # the WHOLE point: prod pulls the retained tag, not the pruned short sha
    assert calls["fixed"]["image_ref"] == "v1.2.3"
    assert res.detail["image_ref"] == "v1.2.3"
    assert calls["preview"] is None


def test_prod_release_branch_pulls_latest_tag(calls):
    res = _deploy(
        deploy_type="prod",
        version_ref="release/0.1",
        staging_validated=True,
        code_reviewed=True,
    )
    assert calls["fixed"]["image_ref"] == "v9.9.9"  # the line's latest tag
    assert res.backend == "deploy-primitive"


@pytest.mark.parametrize("bad", ["main", "deadbeef", "c" * 40])
def test_prod_rejects_code_forms_fail_closed(calls, bad):
    with pytest.raises(ValueError, match="does not accept"):
        _deploy(
            deploy_type="prod",
            version_ref=bad,
            staging_validated=True,
            code_reviewed=True,
        )
    assert calls["fixed"] is None  # fails before any backend


# --- staging: mirrors prod but permissive (code OR release) ----------------


@pytest.mark.parametrize(
    "version_ref,expect_image",
    [("main", SHA_CODE[:7]), ("c" * 40, SHA_CODE[:7]), ("v1.2.3", "v1.2.3")],
)
def test_staging_accepts_code_and_release(calls, version_ref, expect_image):
    res = _deploy(deploy_type="staging", version_ref=version_ref)
    assert res.backend == "deploy-primitive"
    assert calls["fixed"]["env"] == "staging"
    assert calls["fixed"]["image_ref"] == expect_image


# --- preview slots: main / pr / commit / tag ------------------------------


def test_preview_branch(calls):
    res = _deploy(deploy_type="preview/branch", version_ref="main")
    assert res.backend == "preview-lifecycle"
    assert res.target.sub_domain == "report-branch-main"
    assert calls["preview"]["kind"] == "branch" and calls["preview"]["value"] == "main"
    assert calls["preview"]["image_ref"] == SHA_CODE[:7]


def test_preview_branch_defaults_version_ref_to_main(calls):
    # CF2: branch is definitionally a tip — version_ref omitted defaults to main
    res = _deploy(deploy_type="preview/branch", version_ref="")
    assert res.target.sub_domain == "report-branch-main"


def test_preview_pr_uses_resolve_pr_and_pr_slot(calls):
    res = _deploy(deploy_type="preview/pr", version_ref=7)
    assert res.target.sub_domain == "report-pr-7"
    assert calls["preview"]["kind"] == "pr" and calls["preview"]["value"] == 7
    assert calls["preview"]["code"] == SHA_CODE


def test_preview_pr_rejects_non_numeric(calls):
    with pytest.raises(ValueError, match="PR number"):
        _deploy(deploy_type="preview/pr", version_ref="main")
    assert calls["preview"] is None


def test_preview_commit_slot_is_short_sha(calls):
    res = _deploy(deploy_type="preview/commit", version_ref="c" * 40)
    assert res.target.sub_domain == "report-commit-ccccccc"
    assert calls["preview"]["kind"] == "commit"


def test_preview_commit_rejects_a_branch(calls):
    # CF3: branch and commit are now distinct types — commit takes ONLY a sha
    with pytest.raises(ValueError, match="does not accept a 'branch'"):
        _deploy(deploy_type="preview/commit", version_ref="main")
    assert calls["preview"] is None


def test_preview_commit_rejects_short_sha_with_surface_message(calls):
    # CF4: a short sha resolves to itself (not a full commit) -> clear, version_ref-level error
    with pytest.raises(ValueError, match="not a full commit sha"):
        _deploy(deploy_type="preview/commit", version_ref="abc1234")
    assert calls["preview"] is None


def test_preview_tag_slot_is_dns_safe_and_pulls_tag(calls):
    res = _deploy(deploy_type="preview/tag", version_ref="v1.2.3")
    assert res.target.sub_domain == "report-tag-v1-2-3"
    assert calls["preview"]["image_ref"] == "v1.2.3"  # release image, not a sha


# --- canary: any code, fixed reserved slot --------------------------------


def test_canary_runs_code_on_the_reserved_slot(calls):
    res = _deploy(deploy_type="canary", version_ref="main")
    assert res.backend == "preview-lifecycle"
    assert calls["preview"]["kind"] == "pr"
    assert calls["preview"]["value"] == dv2._CANARY_PR
    assert res.target.sub_domain == f"report-pr-{dv2._CANARY_PR}"


def test_canary_defaults_version_ref_to_main(calls):
    _deploy(deploy_type="canary", version_ref="")
    assert calls["preview"]["code"] == SHA_CODE  # resolved 'main'


# --- iac_ref drives the clone (iac_branch dissolved) ----------------------


def test_iac_branch_ref_is_cloned_verbatim(calls):
    _deploy(deploy_type="preview/pr", version_ref=7, iac_ref="release/1.2")
    assert calls["preview"]["branch"] == "release/1.2"


def test_iac_sha_falls_back_to_default_branch(calls):
    # a sha can't be `git clone -b`'d (#342) -> default branch; iac_ref stays the record
    res = _deploy(deploy_type="preview/pr", version_ref=7, iac_ref="d" * 40)
    assert calls["preview"]["branch"] == "main"
    assert res.target.iac_ref == SHA_IAC


# --- gates ------------------------------------------------------------------


def test_prod_requires_staging_first(calls):
    with pytest.raises(ValueError, match="requires a prior staging"):
        _deploy(deploy_type="prod", version_ref="v1.2.3", code_reviewed=True)
    assert calls["fixed"] is None


def test_prod_break_glass_bypasses_staging_first(calls):
    res = _deploy(
        deploy_type="prod",
        version_ref="v1.2.3",
        break_glass=True,
        code_reviewed=True,
    )
    assert res.backend == "deploy-primitive"


@pytest.mark.parametrize("code_reviewed", [False, None])
def test_prod_data_fails_closed_without_positive_review(calls, code_reviewed):
    kwargs = dict(deploy_type="prod", version_ref="v1.2.3", staging_validated=True)
    if code_reviewed is not None:
        kwargs["code_reviewed"] = code_reviewed
    with pytest.raises(ValueError, match="RL-DATA-1"):
        _deploy(**kwargs)
    assert calls["fixed"] is None


def test_unknown_type_rejected(calls):
    with pytest.raises(ValueError, match="unknown deploy type"):
        _deploy(deploy_type="bogus", version_ref="main")
    assert calls["fixed"] is None and calls["preview"] is None


def test_unknown_service_rejected(calls):
    # platform/postgres is now a KNOWN (derived) service — use one with no deploy.py
    with pytest.raises(ValueError, match="unknown service"):
        _deploy(service="platform/does-not-exist", deploy_type="staging", version_ref="main")
    assert calls["fixed"] is None


# --- data lane / red-line helpers (unchanged contract) ---------------------


def test_resolve_data_lane_by_env():
    from tools.deploy_contract import make_deploy_target

    def t(env, **kw):
        return make_deploy_target(
            service="finance_report/app",
            env=env,
            code_version=SHA_CODE,
            iac_ref=SHA_IAC,
            **kw,
        )

    assert resolve_data_lane(t("prod")) == "prod"
    assert resolve_data_lane(t("staging")) == "staging"
    assert resolve_data_lane(t("preview", alias_kind="branch", alias_value="main")) == "staging"


def test_enforce_returns_data_lane():
    from tools.deploy_contract import make_deploy_target

    target = make_deploy_target(
        service="finance_report/app", env="prod", code_version=SHA_CODE, iac_ref=SHA_IAC
    )
    assert enforce_data_lane_red_lines(target, code_reviewed=True) == "prod"


# --- CLI entry (the cutover seam) ------------------------------------------


@pytest.fixture
def cli(monkeypatch):
    """Drive deploy_v2.main with client + deploy_v2 faked — no resolve, no Dokploy."""
    import json

    from tools.deploy_contract import make_target

    rec = {}
    import libs.dokploy as dk

    monkeypatch.setattr(dk, "get_dokploy", lambda host: f"client@{host}")

    def fake_deploy_v2(**kw):
        rec.update(kw)
        target = make_target(
            kw["deploy_type"], service=kw["service"], version=SHA_CODE, iac_ref=SHA_IAC
        )
        return DeployV2Result(target, "staging", "deploy-primitive", {"sha": SHA_CODE})

    monkeypatch.setattr(dv2, "deploy_v2", fake_deploy_v2)
    return rec, json


def test_cli_passes_surface_through(cli, capsys):
    rec, json = cli
    rc = dv2.main(
        ["--type", "staging", "--version-ref", "main", "--iac-ref", "main",
         "--domain", "zp.io"]
    )
    assert rc == 0
    assert rec["deploy_type"] == "staging"
    assert rec["version_ref"] == "main" and rec["iac_ref"] == "main"
    assert rec["client"] == "client@cloud.zp.io"  # host = cloud.<domain>
    out = json.loads(capsys.readouterr().out)
    assert out["env"] == "staging" and out["backend"] == "deploy-primitive"


def test_cli_code_reviewed_flag_maps_to_true_else_none(cli):
    rec, _ = cli
    dv2.main(["--type", "staging", "--version-ref", "m", "--iac-ref", "m", "--domain", "zp.io"])
    assert rec["code_reviewed"] is None  # omitted stays deny-by-default
    dv2.main(
        ["--type", "prod", "--version-ref", "v1.0.0", "--iac-ref", "m", "--domain",
         "zp.io", "--staging-validated", "--code-reviewed"]
    )
    assert rec["code_reviewed"] is True  # explicit positive signal


def test_cli_reports_deploy_failure(monkeypatch, capsys):
    def boom(**kw):
        raise ValueError("does not accept a 'branch' version_ref")

    import libs.dokploy as dk

    monkeypatch.setattr(dk, "get_dokploy", lambda host: object())
    monkeypatch.setattr(dv2, "deploy_v2", boom)
    rc = dv2.main(
        ["--type", "prod", "--version-ref", "main", "--iac-ref", "m", "--domain", "zp.io"]
    )
    assert rc == 1
    assert "deploy_v2 failed" in capsys.readouterr().err


# --- prod parity: Vault-TTL preflight + config verification + model overrides ----


def test_fixed_deploy_verifies_vault_and_config_by_default(calls):
    # parity with the retired bash dokploy_deploy.sh: the unified path must KEEP the
    # VAULT_APP_TOKEN TTL preflight + post-deploy IAC_CONFIG_HASH check (default-ON).
    _deploy(deploy_type="staging", version_ref="main")
    assert calls["fixed"]["verify_vault"] is True
    assert calls["fixed"]["verify_config"] is True
    assert "model_overrides" in calls["fixed"]  # threaded through (env-sourced)


def test_fixed_deploy_verify_can_be_disabled(calls):
    _deploy(deploy_type="staging", version_ref="main", verify_vault=False, verify_config=False)
    assert calls["fixed"]["verify_vault"] is False
    assert calls["fixed"]["verify_config"] is False


def test_cli_verify_flags_default_on_and_flip(cli):
    rec, _ = cli
    dv2.main(["--type", "staging", "--version-ref", "main", "--iac-ref", "main", "--domain", "zp.io"])
    assert rec["verify_vault"] is True and rec["verify_config"] is True
    dv2.main(
        ["--type", "staging", "--version-ref", "main", "--iac-ref", "main", "--domain",
         "zp.io", "--skip-vault-check", "--no-verify-config"]
    )
    assert rec["verify_vault"] is False and rec["verify_config"] is False


# --- platform services route to the iac_runner webhook (iac_pinned) ---------


def _platform(monkeypatch, *, poll_status="completed", **over):
    sent = {}
    monkeypatch.setattr(
        dv2, "trigger_platform_deploy",
        lambda **kw: sent.update(kw) or {"status": "accepted", "deployment_id": "d1"},
    )
    monkeypatch.setattr(
        dv2, "poll_platform_deploy_status",
        lambda **kw: {"status": poll_status, "deployment_id": "d1"},
    )
    monkeypatch.setattr(dv2, "resolve_to_sha", lambda ref, **kw: SHA_IAC)
    base = dict(
        service="platform/redis", deploy_type="staging", version_ref="ignored",
        iac_ref="main", client=object(), domain="zitian.party",
    )
    base.update(over)
    return sent, deploy_v2(**base)


def test_platform_service_routes_to_iac_runner(monkeypatch):
    sent, res = _platform(monkeypatch)
    assert res.backend == "iac-runner"
    assert sent["env"] == "staging"
    assert sent["ref"] == SHA_IAC  # deploy ref IS the iac_ref sha
    assert sent["services"] == ["platform/redis"]
    assert res.detail["iac_runner"]["status"] == "accepted"
    assert res.target.code_version == SHA_IAC  # platform version identity = the iac commit


def test_platform_prod_maps_to_env_production(monkeypatch):
    sent, _res = _platform(
        monkeypatch, deploy_type="prod", version_ref="", code_reviewed=True
    )
    assert sent["env"] == "production"


def test_platform_prod_requires_code_reviewed(monkeypatch):
    # RL-DATA-1 is deny-by-default for platform prod too (postgres/etc. sit on prod data)
    with pytest.raises(ValueError, match="RL-DATA-1"):
        _platform(monkeypatch, deploy_type="prod", version_ref="")


def test_platform_rejects_preview_type(monkeypatch):
    with pytest.raises(ValueError, match="staging/prod only"):
        _platform(monkeypatch, deploy_type="preview/pr", version_ref=5)


def test_platform_ignores_version_ref(monkeypatch):
    # version_ref must NOT be resolved for an iac-pinned service
    def boom(*a, **k):
        raise AssertionError("platform must not resolve version_ref")

    monkeypatch.setattr(dv2, "resolve_image_ref", boom)
    monkeypatch.setattr(dv2, "resolve_pr", boom)
    sent, _res = _platform(monkeypatch, version_ref="whatever-garbage")
    assert sent["ref"] == SHA_IAC


def test_platform_wait_polls_and_records_final(monkeypatch):
    _sent, res = _platform(monkeypatch, poll_status="completed")
    assert res.detail["iac_runner_final"]["status"] == "completed"


def test_platform_wait_raises_on_failed_deploy(monkeypatch):
    with pytest.raises(RuntimeError, match="ended 'failed'"):
        _platform(monkeypatch, poll_status="failed")
