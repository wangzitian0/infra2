"""Infra-009: unified deploy front door (deploy_v2) — the (type × version_ref) matrix.

Tests routing / form-gating / image_ref threading / gates / data-lane red lines. The
network resolvers (resolve_image_ref / resolve_pr / resolve_to_sha) and the backends
(libs.deploy.preview.up, libs.deploy.promote.deploy) are monkeypatched, so NO git/Dokploy/HTTP
call happens — classify_ref stays real, so form validation is exercised for real.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
import pytest

import tools.deploy_v2 as dv2
from tools.deploy_v2 import (
    DeployV2Result,
    assert_iac_ref_on_main,
    deploy_v2,
    enforce_data_lane_red_lines,
    resolve_data_lane,
)
from tools.resolve_deploy_ref import ResolvedRef, classify_ref

SHA_CODE = "c" * 40
SHA_IAC = "d" * 40


@pytest.fixture(autouse=True)
def _stub_iac_on_main_guard(monkeypatch):
    # The #465 on-main guard calls GitHub's compare API. Stub it module-wide so routing
    # tests (app and platform) never network; the guard's own behavior is covered by the
    # assert_iac_ref_on_main unit tests, which call the real imported function (a local name
    # unaffected by this monkeypatch on the dv2 module attribute).
    monkeypatch.setattr(dv2, "assert_iac_ref_on_main", lambda *a, **k: None)


def _fake_resolve_image_ref(ref, **_kw):
    """Resolve like the real one (form via real classify_ref) but with no network.

    A release pulls its tag (image_ref == the tag); code pulls the short sha. A bare sha
    passes through as-is (mirrors resolve_to_sha — short shas are NOT expanded), so the
    front door's full-sha guard (CF4) is exercised for real.
    """
    form = classify_ref(ref)
    if form == "tag":
        return ResolvedRef(sha=SHA_CODE, image_ref=ref.strip(), form=form)
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
    rec = {"preview": None, "fixed": None, "image_waits": []}

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

    def fake_wait(spec, image_ref, **kw):
        rec["image_waits"].append(
            {
                "service": spec.key,
                "repositories": spec.image_repositories,
                "image_ref": image_ref,
                **kw,
            }
        )

    monkeypatch.setattr(dv2, "_preview_up", fake_up)
    monkeypatch.setattr(dv2, "_deploy_fixed", fake_deploy)
    monkeypatch.setattr(dv2, "_wait_for_image_dependencies", fake_wait)
    monkeypatch.setattr(dv2, "resolve_image_ref", _fake_resolve_image_ref)
    monkeypatch.setattr(dv2, "resolve_pr", _fake_resolve_pr)
    monkeypatch.setattr(dv2, "resolve_to_sha", lambda ref, **kw: SHA_IAC)
    return rec


def _deploy(**over):
    # Fixed envs accept a tag iac_ref only; preview/canary clone a live ref. Default the
    # iac_ref to match the type so callers only pass it when the test is about iac_ref itself.
    fixed = over.get("deploy_type") in ("staging", "prod")
    base = dict(
        service="finance_report/app",
        iac_ref="v0.0.0" if fixed else "main",
        client=object(),
        domain="zitian.party",
    )
    base.update(over)
    return deploy_v2(**base)


# --- per-service source repo resolution (truealpha's first-ever v0.0.3 staging deploy
# resolved its tag against finance_report's repo — the sole hardcoded default — colliding
# with finance_report's own unrelated, ancient v0.0.3 tag) ------------------


def test_repo_for_service_maps_known_services():
    assert dv2._repo_for_service("finance_report/app") == dv2._APP_REPO
    assert (
        dv2._repo_for_service("truealpha/app")
        == "https://github.com/wangzitian0/truealpha.git"
    )


def test_repo_for_service_falls_back_to_app_repo_for_unknown_service():
    assert dv2._repo_for_service("some/unregistered-service") == dv2._APP_REPO


def test_deploy_resolves_version_ref_against_the_services_own_repo(monkeypatch, calls):
    seen_repos = []

    def recording_resolve_image_ref(ref, **kw):
        seen_repos.append(kw.get("repo"))
        return _fake_resolve_image_ref(ref, **kw)

    monkeypatch.setattr(dv2, "resolve_image_ref", recording_resolve_image_ref)
    _deploy(service="truealpha/app", deploy_type="staging", version_ref="v0.0.3")
    assert seen_repos == ["https://github.com/wangzitian0/truealpha.git"]


def test_deploy_repo_override_wins_over_service_default(monkeypatch, calls):
    seen_repos = []

    def recording_resolve_image_ref(ref, **kw):
        seen_repos.append(kw.get("repo"))
        return _fake_resolve_image_ref(ref, **kw)

    monkeypatch.setattr(dv2, "resolve_image_ref", recording_resolve_image_ref)
    _deploy(
        deploy_type="staging",
        version_ref="v0.0.3",
        repo="https://example.invalid/other.git",
    )
    assert seen_repos == ["https://example.invalid/other.git"]


# --- #465: app→infra on-main compatibility guard (assert_iac_ref_on_main) ---


class _CmpResp:
    def __init__(self, status, code=200):
        self._status = status
        self.status_code = code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "err",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return {"status": self._status}


def _cmp_transport(status, code=200):
    def transport(url, **_kw):
        return _CmpResp(status, code)

    return transport


@pytest.mark.parametrize("status", ["behind", "identical"])
def test_iac_ref_on_main_accepts_reachable(status):
    # tag reachable from infra2 main -> ok (no raise)
    assert_iac_ref_on_main("v1.2.3", "prod", token="", transport=_cmp_transport(status))


@pytest.mark.parametrize("status", ["ahead", "diverged"])
def test_iac_ref_off_main_refused(status):
    with pytest.raises(ValueError, match="not on infra2 main"):
        assert_iac_ref_on_main("v1.2.3", "prod", token="", transport=_cmp_transport(status))


def test_iac_ref_on_main_exempt_for_preview():
    # preview/canary clone live refs -> the API is never called
    called = []

    def transport(url, **_kw):
        called.append(url)
        return _CmpResp("behind")

    assert_iac_ref_on_main("main", "preview/branch", token="", transport=transport)
    assert called == []


def test_iac_ref_on_main_fail_closed_on_api_error():
    # a transport/API failure must raise (fail-closed), not let an unverified ref through
    with pytest.raises(httpx.HTTPStatusError):
        assert_iac_ref_on_main(
            "v1.2.3", "prod", token="", transport=_cmp_transport("behind", code=502)
        )


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


@pytest.mark.parametrize("bad", ["main", "deadbeef", "c" * 40, "release/0.1"])
def test_prod_rejects_non_tag_version_ref_fail_closed(calls, bad):
    # prod accepts a release TAG only; a code ref (or the retired release-branch form)
    # fails closed before any backend. release/0.1 is now an unrecognized ref entirely.
    with pytest.raises(ValueError, match="does not accept|unrecognized deploy ref"):
        _deploy(
            deploy_type="prod",
            version_ref=bad,
            staging_validated=True,
            code_reviewed=True,
        )
    assert calls["fixed"] is None  # fails before any backend


# --- staging: mirrors prod — release TAG only (promote-not-rebuild) --------


def test_staging_accepts_tag_and_pulls_it(calls):
    res = _deploy(deploy_type="staging", version_ref="v1.2.3")
    assert res.backend == "deploy-primitive"
    assert calls["fixed"]["env"] == "staging"
    assert calls["fixed"]["image_ref"] == "v1.2.3"
    assert calls["image_waits"][-1]["image_ref"] == "v1.2.3"


@pytest.mark.parametrize("bad", ["main", "c" * 40, "release/0.1"])
def test_staging_rejects_code_forms_fail_closed(calls, bad):
    with pytest.raises(ValueError, match="does not accept|unrecognized deploy ref"):
        _deploy(deploy_type="staging", version_ref=bad)
    assert calls["fixed"] is None


# --- preview slots: main / pr / commit / tag ------------------------------


def test_preview_branch(calls):
    res = _deploy(deploy_type="preview/branch", version_ref="main")
    assert res.backend == "preview-lifecycle"
    assert res.target.sub_domain == "report-branch-main"
    assert calls["preview"]["kind"] == "branch" and calls["preview"]["value"] == "main"
    assert calls["preview"]["image_ref"] == SHA_CODE[:7]
    assert calls["image_waits"][-1]["repositories"] == (
        "ghcr.io/wangzitian0/finance_report-backend",
        "ghcr.io/wangzitian0/finance_report-frontend",
    )


def test_preview_branch_expected_sha_allows_matching_main(calls):
    res = _deploy(
        deploy_type="preview/branch",
        version_ref="main",
        expected_sha=SHA_CODE,
    )

    assert res.target.code_version == SHA_CODE
    assert calls["preview"]["code"] == SHA_CODE


def test_preview_branch_expected_sha_rejects_mismatch_before_side_effect(calls):
    with pytest.raises(ValueError, match="not expected sha"):
        _deploy(
            deploy_type="preview/branch",
            version_ref="main",
            expected_sha="e" * 40,
        )

    assert calls["preview"] is None
    assert calls["image_waits"] == []


def test_image_readiness_failure_stops_before_preview_side_effect(calls, monkeypatch):
    def boom(*_args, **_kw):
        raise RuntimeError("required image artifacts")

    monkeypatch.setattr(dv2, "_wait_for_image_dependencies", boom)

    with pytest.raises(RuntimeError, match="required image artifacts"):
        _deploy(deploy_type="preview/branch", version_ref="main")

    assert calls["preview"] is None and calls["fixed"] is None


def test_image_readiness_failure_stops_before_fixed_side_effect(calls, monkeypatch):
    def boom(*_args, **_kw):
        raise RuntimeError("required image artifacts")

    monkeypatch.setattr(dv2, "_wait_for_image_dependencies", boom)

    with pytest.raises(RuntimeError, match="required image artifacts"):
        _deploy(deploy_type="staging", version_ref="v1.2.3")

    assert calls["preview"] is None and calls["fixed"] is None


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
    assert calls["image_waits"][-1]["image_ref"] == "v1.2.3"


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
    _deploy(deploy_type="preview/pr", version_ref=7, iac_ref="v1.2.3")
    assert calls["preview"]["branch"] == "v1.2.3"


def test_iac_sha_falls_back_to_default_branch(calls):
    # a sha can't be `git clone -b`'d (#342) -> default branch; iac_ref stays the record
    res = _deploy(deploy_type="preview/pr", version_ref=7, iac_ref="d" * 40)
    assert calls["preview"]["branch"] == "main"
    assert res.target.iac_ref == SHA_IAC


@pytest.mark.parametrize("bad_iac", ["main", "d" * 40])
def test_fixed_env_rejects_non_tag_iac_ref(calls, bad_iac):
    # staging/prod pin IaC to a release tag; a branch/sha iac_ref fails closed BEFORE any
    # backend (the gap that let a main-sha reconcile auto-deploy platform to prod).
    with pytest.raises(ValueError, match="requires a release-tag iac_ref"):
        _deploy(
            deploy_type="prod",
            version_ref="v1.2.3",
            iac_ref=bad_iac,
            staging_validated=True,
            code_reviewed=True,
        )
    assert calls["fixed"] is None


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


def test_truealpha_staging_routes_fixed_with_its_own_service(calls):
    # #500: a second bespoke SERVICES entry routes through the same fixed-compose path,
    # carrying its own service key through to the backend (not finance_report's).
    res = _deploy(
        service="truealpha/app",
        deploy_type="staging",
        version_ref="v0.0.2",
        iac_ref="v0.0.0",
    )
    assert calls["fixed"]["service"] == "truealpha/app"
    assert calls["preview"] is None
    assert res.target.service == "truealpha/app"


def test_truealpha_preview_routes_to_preview_backend_with_its_own_service(calls):
    # #522: truealpha/app now has its own preview compose (ServiceSpec.supports_preview=
    # True, deploy_env_config.preview_service_config("truealpha/app")) — it must route
    # through the SAME preview backend as finance_report/app, but carrying its own
    # service key so the backend resolves truealpha's project/compose/DB, never
    # finance_report's internals.
    _deploy(
        service="truealpha/app",
        deploy_type="preview/branch",
        version_ref="main",
    )
    assert calls["fixed"] is None
    assert calls["preview"]["service"] == "truealpha/app"


def test_truealpha_canary_routes_to_preview_backend_too(calls):
    _deploy(service="truealpha/app", deploy_type="canary", version_ref="main")
    assert calls["fixed"] is None
    assert calls["preview"]["service"] == "truealpha/app"


def test_unknown_service_rejected(calls):
    # platform/postgres is now a KNOWN (derived) service — use one with no deploy.py
    with pytest.raises(ValueError, match="unknown service"):
        _deploy(
            service="platform/does-not-exist", deploy_type="staging", version_ref="main"
        )
    assert calls["fixed"] is None


# --- data lane / red-line helpers (unchanged contract) ---------------------


def test_resolve_data_lane_by_env():
    from libs.deploy_contract import make_deploy_target

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
    assert (
        resolve_data_lane(t("preview", alias_kind="branch", alias_value="main"))
        == "staging"
    )


def test_enforce_returns_data_lane():
    from libs.deploy_contract import make_deploy_target

    target = make_deploy_target(
        service="finance_report/app", env="prod", code_version=SHA_CODE, iac_ref=SHA_IAC
    )
    assert enforce_data_lane_red_lines(target, code_reviewed=True) == "prod"


# --- CLI entry (the cutover seam) ------------------------------------------


@pytest.fixture
def cli(monkeypatch):
    """Drive deploy_v2.main with client + deploy_v2 faked — no resolve, no Dokploy."""
    import json

    from libs.deploy_contract import make_target

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
        [
            "--type",
            "staging",
            "--version-ref",
            "main",
            "--iac-ref",
            "main",
            "--domain",
            "zp.io",
        ]
    )
    assert rc == 0
    assert rec["deploy_type"] == "staging"
    assert rec["version_ref"] == "main" and rec["iac_ref"] == "main"
    assert rec["client"] == "client@cloud.zp.io"  # host = cloud.<domain>
    assert rec["image_wait_seconds"] is None
    assert rec["image_poll_seconds"] is None
    out = json.loads(capsys.readouterr().out)
    assert out["env"] == "staging" and out["backend"] == "deploy-primitive"


def test_cli_dokploy_host_ignores_a_per_service_domain_override(cli, monkeypatch):
    # #550 regression: truealpha/app's own public --domain (truealpha.club) must never
    # redirect the Dokploy CONTROL-PLANE host — there is exactly one Dokploy instance,
    # always reachable via INTERNAL_DOMAIN (org-wide, set by both deploy workflows,
    # never per-service-overridden). --domain still flows through as the app's own
    # public domain (rec["domain"]) — only the Dokploy client host is decoupled from it.
    rec, _json = cli
    monkeypatch.setenv("INTERNAL_DOMAIN", "zitian.party")
    rc = dv2.main(
        [
            "--service",
            "truealpha/app",
            "--type",
            "staging",
            "--version-ref",
            "main",
            "--iac-ref",
            "main",
            "--domain",
            "truealpha.club",
        ]
    )
    assert rc == 0
    assert rec["client"] == "client@cloud.zitian.party"
    assert rec["domain"] == "truealpha.club"


def test_cli_dokploy_host_falls_back_to_domain_flag_without_internal_domain(
    cli, monkeypatch
):
    # Local/manual runs that don't export INTERNAL_DOMAIN keep today's behavior.
    rec, _json = cli
    monkeypatch.delenv("INTERNAL_DOMAIN", raising=False)
    rc = dv2.main(
        ["--type", "staging", "--version-ref", "main", "--iac-ref", "main", "--domain", "zp.io"]
    )
    assert rc == 0
    assert rec["client"] == "client@cloud.zp.io"


def test_cli_passes_image_wait_overrides(cli):
    rec, _json = cli
    rc = dv2.main(
        [
            "--type",
            "staging",
            "--version-ref",
            "main",
            "--iac-ref",
            "main",
            "--domain",
            "zp.io",
            "--image-wait-seconds",
            "42",
            "--image-poll-seconds",
            "3",
        ]
    )

    assert rc == 0
    assert rec["image_wait_seconds"] == 42
    assert rec["image_poll_seconds"] == 3


def test_cli_code_reviewed_flag_maps_to_true_else_none(cli):
    rec, _ = cli
    dv2.main(
        [
            "--type",
            "staging",
            "--version-ref",
            "m",
            "--iac-ref",
            "m",
            "--domain",
            "zp.io",
        ]
    )
    assert rec["code_reviewed"] is None  # omitted stays deny-by-default
    dv2.main(
        [
            "--type",
            "prod",
            "--version-ref",
            "v1.0.0",
            "--iac-ref",
            "m",
            "--domain",
            "zp.io",
            "--staging-validated",
            "--code-reviewed",
        ]
    )
    assert rec["code_reviewed"] is True  # explicit positive signal


def test_cli_reports_deploy_failure(monkeypatch, capsys):
    def boom(**kw):
        raise ValueError("does not accept a 'branch' version_ref")

    import libs.dokploy as dk

    monkeypatch.setattr(dk, "get_dokploy", lambda host: object())
    monkeypatch.setattr(dv2, "deploy_v2", boom)
    rc = dv2.main(
        [
            "--type",
            "prod",
            "--version-ref",
            "main",
            "--iac-ref",
            "m",
            "--domain",
            "zp.io",
        ]
    )
    assert rc == 1
    assert "deploy_v2 failed" in capsys.readouterr().err


# --- prod parity: Vault-TTL preflight + config verification + model overrides ----


def test_fixed_deploy_verifies_vault_and_config_by_default(calls):
    # parity with the retired bash dokploy_deploy.sh: the unified path must KEEP the
    # VAULT_APP_TOKEN TTL preflight + post-deploy IAC_CONFIG_HASH check (default-ON).
    _deploy(deploy_type="staging", version_ref="v1.2.3")
    assert calls["fixed"]["verify_vault"] is True
    assert calls["fixed"]["verify_config"] is True
    assert "model_overrides" in calls["fixed"]  # threaded through (env-sourced)


def test_fixed_deploy_verify_can_be_disabled(calls):
    _deploy(
        deploy_type="staging",
        version_ref="v1.2.3",
        verify_vault=False,
        verify_config=False,
    )
    assert calls["fixed"]["verify_vault"] is False
    assert calls["fixed"]["verify_config"] is False


# --- teardown: the --down flag folds in the retired preview-lifecycle `down` ------


def _fake_down_result(kind, value, *, domain, client):
    from types import SimpleNamespace

    return SimpleNamespace(
        action="down",
        alias=f"{kind}-{value}",
        compose_id="cmp-1",
        url=f"https://report-{kind}-{value}.{domain}",
    )


def test_cli_down_tears_down_the_selected_preview_alias(monkeypatch, capsys):
    # --down resolves the SAME alias (kind from --type, value from --version-ref) and
    # routes to the preview backend's down(); it never resolves a ref or deploys.
    rec = {}

    def fake_down(kind, value, *, domain, client, service):
        rec.update(kind=kind, value=value, domain=domain, client=client, service=service)
        return _fake_down_result(kind, value, domain=domain, client=client)

    import libs.dokploy as dk

    monkeypatch.setattr(dk, "get_dokploy", lambda host: f"client@{host}")
    monkeypatch.setattr(dv2, "_preview_down", fake_down)
    # deploy_v2 must NOT be called on the teardown path.
    monkeypatch.setattr(
        dv2,
        "deploy_v2",
        lambda **kw: pytest.fail("deploy_v2 must not run for --down"),
    )

    rc = dv2.main(
        [
            "--type",
            "preview/pr",
            "--version-ref",
            "5",
            "--iac-ref",
            "main",
            "--domain",
            "zp.io",
            "--down",
        ]
    )

    assert rc == 0
    assert rec == {
        "kind": "pr",
        "value": "5",
        "domain": "zp.io",
        "client": "client@cloud.zp.io",
        "service": "finance_report/app",
    }
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "down" and out["alias"] == "pr-5"


def test_cli_down_rejects_a_fixed_env(monkeypatch, capsys):
    # staging/prod have no ephemeral alias to remove — --down must fail closed.
    import libs.dokploy as dk

    monkeypatch.setattr(
        dk,
        "get_dokploy",
        lambda host: pytest.fail("must not build a client for an invalid --down"),
    )
    rc = dv2.main(
        [
            "--type",
            "staging",
            "--version-ref",
            "v1.2.3",
            "--iac-ref",
            "v1.2.3",
            "--domain",
            "zp.io",
            "--down",
        ]
    )
    assert rc == 1
    assert "--down only tears down preview" in capsys.readouterr().err


def test_cli_down_rejects_a_malformed_domain(monkeypatch, capsys):
    # a whitespace/empty domain would corrupt cloud.<domain>; --down must reject it
    # before building the Dokploy client — the same guard the preview backend applies on up.
    import libs.dokploy as dk

    monkeypatch.setattr(
        dk,
        "get_dokploy",
        lambda host: pytest.fail(
            "must not build a client for a malformed --down domain"
        ),
    )
    rc = dv2.main(
        [
            "--type",
            "preview/branch",
            "--version-ref",
            "main",
            "--iac-ref",
            "main",
            "--domain",
            "bad domain",
            "--down",
        ]
    )
    assert rc == 1
    assert "invalid domain" in capsys.readouterr().err


def test_cli_verify_flags_default_on_and_flip(cli):
    rec, _ = cli
    dv2.main(
        [
            "--type",
            "staging",
            "--version-ref",
            "main",
            "--iac-ref",
            "main",
            "--domain",
            "zp.io",
        ]
    )
    assert rec["verify_vault"] is True and rec["verify_config"] is True
    dv2.main(
        [
            "--type",
            "staging",
            "--version-ref",
            "main",
            "--iac-ref",
            "main",
            "--domain",
            "zp.io",
            "--skip-vault-check",
            "--no-verify-config",
        ]
    )
    assert rec["verify_vault"] is False and rec["verify_config"] is False


# --- platform services route to the iac_runner webhook (iac_pinned) ---------


def _platform(monkeypatch, *, poll_status="completed", **over):
    sent = {}
    monkeypatch.setattr(
        dv2,
        "trigger_platform_deploy",
        lambda **kw: sent.update(kw) or {"status": "accepted", "deployment_id": "d1"},
    )
    monkeypatch.setattr(
        dv2,
        "poll_platform_deploy_status",
        lambda **kw: {"status": poll_status, "deployment_id": "d1"},
    )
    monkeypatch.setattr(dv2, "resolve_to_sha", lambda ref, **kw: SHA_IAC)
    base = dict(
        service="platform/redis",
        deploy_type="staging",
        version_ref="ignored",
        iac_ref="v0.0.0",  # staging/prod pin IaC to a release tag
        client=object(),
        domain="zitian.party",
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
    assert (
        res.target.code_version == SHA_IAC
    )  # platform version identity = the iac commit


def test_platform_wait_uses_timeout_budget_for_status_poll(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        dv2,
        "trigger_platform_deploy",
        lambda **kw: {"status": "accepted", "deployment_id": "d1"},
    )
    monkeypatch.setattr(
        dv2,
        "poll_platform_deploy_status",
        lambda **kw: seen.update(kw) or {"status": "completed", "deployment_id": "d1"},
    )
    monkeypatch.setattr(dv2, "resolve_to_sha", lambda ref, **kw: SHA_IAC)

    deploy_v2(
        service="platform/redis",
        deploy_type="staging",
        version_ref="ignored",
        iac_ref="v0.0.0",
        client=object(),
        domain="zitian.party",
        timeout=120,
    )

    assert seen["attempts"] == 12


def test_platform_batch_cli_routes_services_once(monkeypatch, capsys):
    sent = {}
    monkeypatch.setattr(
        dv2,
        "trigger_platform_deploy",
        lambda **kw: sent.update(kw) or {"status": "accepted", "deployment_id": "d1"},
    )
    monkeypatch.setattr(
        dv2,
        "poll_platform_deploy_status",
        lambda **kw: {"status": "completed", "deployment_id": "d1"},
    )
    monkeypatch.setattr(dv2, "resolve_to_sha", lambda ref, **kw: SHA_IAC)

    rc = dv2.main(
        [
            "--service",
            "platform/redis,platform/alerting",
            "--type",
            "staging",
            "--version-ref",
            "v0.0.0",
            "--iac-ref",
            "v0.0.0",
            "--domain",
            "zitian.party",
            "--timeout",
            "120",
        ]
    )

    assert rc == 0
    assert sent["services"] == ["platform/redis", "platform/alerting"]
    output = json.loads(capsys.readouterr().out)
    assert output["service"] == ["platform/redis", "platform/alerting"]
    assert output["backend"] == "iac-runner"


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


def test_platform_skips_app_image_readiness(monkeypatch):
    def boom(*_args, **_kw):
        raise AssertionError("platform deploys must not wait on app images")

    monkeypatch.setattr(dv2, "_wait_for_image_dependencies", boom)
    sent, res = _platform(monkeypatch)
    assert sent["services"] == ["platform/redis"]
    assert res.backend == "iac-runner"


def test_platform_wait_polls_and_records_final(monkeypatch):
    _sent, res = _platform(monkeypatch, poll_status="completed")
    assert res.detail["iac_runner_final"]["status"] == "completed"


def test_platform_wait_raises_on_failed_deploy(monkeypatch):
    with pytest.raises(RuntimeError, match="ended 'failed'"):
        _platform(monkeypatch, poll_status="failed")


def test_wait_for_image_dependencies_retries_until_all_artifacts_exist(monkeypatch):
    spec = dv2.service_spec("finance_report/app")
    attempts = {"backend": 0, "frontend": 0}
    sleeps = []

    def exists(image, image_ref):
        assert image_ref == "abcdef0"
        if image.endswith("-backend"):
            attempts["backend"] += 1
            return True
        attempts["frontend"] += 1
        return attempts["frontend"] >= 2

    monkeypatch.setattr(dv2, "_image_manifest_exists", exists)
    monkeypatch.setattr(dv2.time, "sleep", lambda seconds: sleeps.append(seconds))

    dv2._wait_for_image_dependencies(spec, "abcdef0", timeout=30, poll_seconds=1)

    assert attempts == {"backend": 2, "frontend": 2}
    assert sleeps == [1]


def test_wait_for_image_dependencies_reports_missing_artifact(monkeypatch):
    spec = dv2.service_spec("finance_report/app")

    monkeypatch.setattr(dv2, "_image_manifest_exists", lambda *_args, **_kw: False)

    with pytest.raises(RuntimeError, match="not published after 0s"):
        dv2._wait_for_image_dependencies(spec, "abcdef0", timeout=0, poll_seconds=0)


@pytest.mark.parametrize(
    "timeout,poll_seconds",
    [(float("nan"), 1), (float("inf"), 1), (1, float("nan")), (1, float("inf"))],
)
def test_wait_for_image_dependencies_rejects_non_finite_overrides(
    timeout, poll_seconds
):
    spec = dv2.service_spec("finance_report/app")

    with pytest.raises(ValueError, match="must be finite"):
        dv2._wait_for_image_dependencies(
            spec, "abcdef0", timeout=timeout, poll_seconds=poll_seconds
        )


def test_wait_for_image_dependencies_rejects_non_finite_env(monkeypatch):
    spec = dv2.service_spec("finance_report/app")

    monkeypatch.setenv("DEPLOY_V2_IMAGE_WAIT_SECONDS", "nan")
    with pytest.raises(ValueError, match="must be finite"):
        dv2._wait_for_image_dependencies(spec, "abcdef0")
