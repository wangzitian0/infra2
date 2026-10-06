"""Infra-009: unified deploy front door (deploy_v2) — the (type × version_ref) matrix.

Tests routing / form-gating / image_ref threading / gates / data-lane red lines. The
network resolvers (resolve_image_ref / resolve_pr / resolve_to_sha) and the backends
(libs.deploy.preview.up, libs.deploy.promote.deploy) are monkeypatched, so NO git/Dokploy/HTTP
call happens — classify_ref stays real, so form validation is exercised for real.
"""

from __future__ import annotations

import ast
import itertools
import json
import subprocess
from pathlib import Path

from libs.deploy import preflight as _preflight
from dataclasses import dataclass

import httpx
import pytest

import tools.deploy_v2 as dv2
from libs.iac_runner_client import (
    RunnerLostDeploymentError,
    status_poll_attempts,
    status_poll_delays,
)
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
    rollback_class: str | None = None


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
    monkeypatch.setattr(dv2, "resolve_branch_to_sha", lambda ref, **kw: SHA_IAC)
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
    def __init__(self, status, code=200, headers=None, text=""):
        self._status = status
        self.status_code = code
        self.headers = headers or {}
        self.text = text

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
        assert_iac_ref_on_main(
            "v1.2.3", "prod", token="", transport=_cmp_transport(status)
        )


def test_iac_ref_on_main_exempt_for_preview():
    # preview/canary clone live refs -> the API is never called
    called = []

    def transport(url, **_kw):
        called.append(url)
        return _CmpResp("behind")

    assert_iac_ref_on_main("main", "preview/branch", token="", transport=transport)
    assert called == []


def test_iac_ref_on_main_fail_closed_on_api_error(empty_checkout):
    # a transport/API failure with no local proof must raise (fail-closed), not let an
    # unverified ref through; the API error stays reachable as the exception cause
    with pytest.raises(RuntimeError, match="compare API failed.*local git") as raised:
        assert_iac_ref_on_main(
            "v1.2.3", "prod", token="", transport=_cmp_transport("behind", code=502)
        )
    assert isinstance(raised.value.__cause__, httpx.HTTPStatusError)


def test_iac_ref_on_main_sends_the_workflow_token(monkeypatch):
    """2026-09-07: two prod promotes died on `403 rate limit exceeded` from the compare API
    because deploy.yml never exported GITHUB_TOKEN, so the call was anonymous (60/hour per
    runner IP). The token from the environment must reach the request (#635)."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
    seen = []

    def transport(url, headers, **_kw):
        seen.append(headers.get("Authorization"))
        return _CmpResp("behind")

    assert_iac_ref_on_main("v1.2.3", "prod", transport=transport)
    assert seen == ["Bearer ghs_test"]


def test_iac_ref_on_main_waits_out_a_short_rate_limit_then_succeeds():
    answers = [
        _CmpResp(
            "",
            code=403,
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1005"},
        ),
        _CmpResp("behind"),
    ]
    slept = []
    assert_iac_ref_on_main(
        "v1.2.3",
        "prod",
        token="t",
        transport=lambda url, **_kw: answers.pop(0),
        sleep=slept.append,
        now=lambda: 1000.0,
    )
    assert slept == [5.0] and answers == []


def test_iac_ref_on_main_fails_closed_and_names_the_missing_token_when_anonymous(
    empty_checkout,
):
    # an anonymous budget resets on the hour: far beyond what a deploy should block on
    resp = _CmpResp(
        "",
        code=403,
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "4600"},
        text="API rate limit exceeded",
    )
    slept = []
    with pytest.raises(RuntimeError, match="UNAUTHENTICATED.*GITHUB_TOKEN"):
        assert_iac_ref_on_main(
            "v1.2.3",
            "prod",
            token="",
            transport=lambda url, **_kw: resp,
            sleep=slept.append,
            now=lambda: 1000.0,
        )
    assert slept == []


def test_iac_ref_on_main_gives_up_after_bounded_attempts(empty_checkout):
    resp = _CmpResp("", code=429, headers={"Retry-After": "2"})
    slept = []
    with pytest.raises(RuntimeError, match="budget is spent"):
        assert_iac_ref_on_main(
            "v1.2.3",
            "prod",
            token="t",
            transport=lambda url, **_kw: resp,
            sleep=slept.append,
            now=lambda: 0.0,
        )
    assert slept == [2.0, 2.0]


# --- #616 item 3: local-git fallback when the compare API cannot answer ---------------
#
# These tests use REAL git (no fake runner): the property under test is what git itself
# says about a full clone, a shallow clone and an empty checkout.

_GIT_IDENTITY = [
    "-c",
    "user.name=test",
    "-c",
    "user.email=test@example.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "tag.gpgsign=false",
]


def _git(cwd, *args):
    subprocess.run(
        ["git", *_GIT_IDENTITY, *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _commit(cwd, name):
    (cwd / name).write_text(name)
    _git(cwd, "add", name)
    _git(cwd, "commit", "-q", "-m", name)


@pytest.fixture
def empty_checkout(tmp_path, monkeypatch):
    """A git repo with no commits: local git cannot answer for any ref."""
    repo = tmp_path / "empty"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    monkeypatch.setattr(_preflight, "REPO_ROOT", repo)
    return repo


@pytest.fixture
def infra2_origin(tmp_path):
    """Origin history: v1.0.0 -> c2 -> c3 (main); side branch c4 carries off-main v9.9.9."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _commit(origin, "c1")
    _git(origin, "tag", "v1.0.0")
    _commit(origin, "c2")
    _commit(origin, "c3")
    _git(origin, "checkout", "-q", "-b", "side")
    _commit(origin, "c4")
    _git(origin, "tag", "v9.9.9")
    _git(origin, "checkout", "-q", "main")
    return origin


@pytest.fixture
def full_clone(infra2_origin, tmp_path, monkeypatch):
    clone = tmp_path / "full"
    _git(tmp_path, "clone", "-q", f"file://{infra2_origin}", str(clone))
    monkeypatch.setattr(_preflight, "REPO_ROOT", clone)
    return clone


@pytest.fixture
def shallow_clone(infra2_origin, tmp_path, monkeypatch):
    clone = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth", "1", f"file://{infra2_origin}", str(clone))
    monkeypatch.setattr(_preflight, "REPO_ROOT", clone)
    return clone


def test_iac_ref_on_main_local_git_proves_ancestry_when_the_api_errors(full_clone):
    """Case (a): the compare API answers 502 and the full clone proves v1.0.0 is an
    ancestor of origin/main, so the guard passes (#616 item 3)."""
    assert_iac_ref_on_main(
        "v1.0.0", "prod", token="", transport=_cmp_transport("behind", code=502)
    )


def test_iac_ref_on_main_local_git_proves_ancestry_when_the_transport_raises(
    full_clone,
):
    def transport(url, **_kw):
        raise httpx.ConnectError("network down")

    assert_iac_ref_on_main("v1.0.0", "staging", token="", transport=transport)


def test_iac_ref_on_main_refused_when_api_errors_and_local_git_says_not_ancestor(
    full_clone,
):
    """Case (b): v9.9.9 exists locally but sits only on the side branch."""
    with pytest.raises(
        RuntimeError, match="compare API failed.*local git.*not reachable"
    ):
        assert_iac_ref_on_main(
            "v9.9.9", "prod", token="", transport=_cmp_transport("behind", code=502)
        )


def test_iac_ref_on_main_refused_when_api_errors_and_the_commit_is_missing_locally(
    full_clone,
):
    """Case (c): a tag that is absent locally cannot be proven, so it is refused."""
    with pytest.raises(
        RuntimeError, match="compare API failed.*local git.*cannot resolve"
    ):
        assert_iac_ref_on_main(
            "v7.7.7", "prod", token="", transport=_cmp_transport("behind", code=502)
        )


def test_iac_ref_on_main_shallow_clone_is_not_trusted_by_accident(shallow_clone):
    """Case (c): the depth-1 clone has origin/main but not the v1.0.0 commit. The answer
    must be a refusal that names both failures, never a silent pass."""
    local_tags = subprocess.run(
        ["git", "tag", "--list"], cwd=shallow_clone, capture_output=True, text=True
    ).stdout.split()
    assert "v1.0.0" not in local_tags  # the fixture really lacks the commit
    with pytest.raises(RuntimeError) as raised:
        assert_iac_ref_on_main(
            "v1.0.0", "prod", token="", transport=_cmp_transport("behind", code=502)
        )
    message = str(raised.value)
    assert "compare API failed" in message
    assert "local git" in message
    assert "cannot resolve 'v1.0.0'" in message


def test_iac_ref_on_main_refused_when_origin_main_is_missing_locally(empty_checkout):
    with pytest.raises(RuntimeError, match="compare API failed.*local git"):
        assert_iac_ref_on_main(
            "v1.0.0", "prod", token="", transport=_cmp_transport("behind", code=502)
        )


def test_iac_ref_on_main_api_verdict_stays_authoritative_over_local_git(full_clone):
    """Case (d): the API answered `ahead`, so local git (which would pass v1.0.0) is not
    consulted to overturn it."""
    with pytest.raises(ValueError, match="not on infra2 main"):
        assert_iac_ref_on_main(
            "v1.0.0", "prod", token="", transport=_cmp_transport("ahead")
        )


def test_iac_ref_on_main_local_git_covers_an_exhausted_rate_limit(full_clone):
    resp = _CmpResp("", code=429, headers={"Retry-After": "2"})
    slept = []
    assert_iac_ref_on_main(
        "v1.0.0",
        "prod",
        token="t",
        transport=lambda url, **_kw: resp,
        sleep=slept.append,
        now=lambda: 0.0,
    )
    assert slept == [2.0, 2.0]


def test_iac_ref_on_main_does_not_touch_local_git_when_the_api_answers(
    tmp_path, monkeypatch
):
    # REPO_ROOT points at a directory that does not exist: any git call would fail loudly
    monkeypatch.setattr(dv2, "REPO_ROOT", tmp_path / "missing", raising=False)
    assert_iac_ref_on_main(
        "v1.0.0", "prod", token="", transport=_cmp_transport("behind")
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
    # truealpha#447: iac_ref must reach the fixed-compose backend as a re-assertable
    # branch/tag, not just get recorded on the target — this is what the backend passes
    # to update_compose(..., branch=...) so the compose's OWN git source stays current.
    assert calls["fixed"]["branch"] == "v0.0.0"  # _deploy's default fixed-env iac_ref
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


# --- #698 / Infra-022 T3.2: promote.deploy()'s ROLLBACK_CLASS must reach deploy_v2's --
# own detail, not be computed by the schema gate and silently discarded ----------------


def test_deploy_v2_carries_rollback_class_into_detail(monkeypatch, calls):
    """The pre-deploy schema gate's ROLLBACK_CLASS (tools.pre_deploy_schema_check.
    classify_rollback, printed by libs.deploy.schema_gate.run_schema_gate) must land in
    deploy_v2's own JSON result -- the thing an operator and the GitHub Actions step
    summary actually see -- not just get computed inside promote.deploy() and dropped."""

    def fake_deploy_with_rollback_class(env, code, **kw):
        return _Plan(
            env=env,
            sha=code,
            compose_id=f"cmp-{env}",
            data="x",
            env_vars={},
            rollback_class="C",
        )

    monkeypatch.setattr(dv2, "_deploy_fixed", fake_deploy_with_rollback_class)
    res = _deploy(deploy_type="staging", version_ref="v1.2.3")
    assert res.detail["rollback_class"] == "C"


def test_deploy_v2_rollback_class_is_none_when_the_gate_never_ran(calls):
    """A service the schema gate doesn't apply to (or a fixture that never wires
    rollback_class) must report None explicitly -- not omit the key, and not fabricate
    a class deploy_v2 itself has no basis for."""
    res = _deploy(deploy_type="staging", version_ref="v1.2.3")
    assert "rollback_class" in res.detail
    assert res.detail["rollback_class"] is None


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
    assert calls["preview"]["kind"] == "canary"
    assert calls["preview"]["value"] == dv2.CANARY_SLOT
    assert res.target.sub_domain == f"report-{dv2.CANARY_SLOT}"


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


def test_preview_iac_sha_can_clone_a_branch_resolving_to_the_same_sha(calls):
    res = _deploy(
        deploy_type="preview/pr",
        version_ref=7,
        iac_ref="d" * 40,
        iac_clone_ref="fix/preview-proof",
    )
    assert calls["preview"]["branch"] == "fix/preview-proof"
    assert res.target.iac_ref == SHA_IAC


def test_preview_iac_clone_ref_rejects_authority_mismatch(monkeypatch, calls):
    monkeypatch.setattr(dv2, "resolve_branch_to_sha", lambda *_a, **_kw: "e" * 40)
    with pytest.raises(ValueError, match="not authoritative iac_ref SHA"):
        _deploy(
            deploy_type="canary",
            version_ref="main",
            iac_ref="d" * 40,
            iac_clone_ref="fix/other-head",
        )
    assert calls["preview"] is None


def test_fixed_env_rejects_iac_clone_ref(calls):
    with pytest.raises(ValueError, match="only supported for preview/canary"):
        _deploy(
            deploy_type="prod",
            version_ref="v1.2.3",
            staging_validated=True,
            code_reviewed=True,
            iac_clone_ref="fix/not-authority",
        )
    assert calls["fixed"] is None


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


def test_enforce_data_lane_snapshot_freshness_warning(monkeypatch, tmp_path, caplog):
    import json
    import logging
    from datetime import datetime, timezone, timedelta
    from libs.deploy_contract import make_deploy_target
    import tools.deploy_v2 as dv2_mod

    old_time = (datetime.now(timezone.utc) - timedelta(days=8)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"generated_at": old_time}), encoding="utf-8")

    target = make_deploy_target(
        service="finance_report/app",
        env="staging",
        code_version=SHA_CODE,
        iac_ref=SHA_IAC,
    )
    with monkeypatch.context() as m:
        orig_path = dv2_mod.Path
        m.setattr(
            dv2_mod,
            "Path",
            lambda p: (
                manifest
                if str(p) == "/data/backups/anonymized/manifest.json"
                else orig_path(p)
            ),
        )
        with caplog.at_level(logging.WARNING, logger="deploy_v2"):
            lane = enforce_data_lane_red_lines(target)
            assert lane == "staging"
            assert "older than 7 days" in caplog.text


# --- CLI entry (the cutover seam) ------------------------------------------


@pytest.fixture
def cli(monkeypatch):
    """Drive deploy_v2.main with client + deploy_v2 faked — no resolve, no Dokploy."""
    import json

    from libs.deploy_contract import make_target

    rec = {}
    import libs.deploy.dokploy_client as dk

    monkeypatch.setattr(dk, "get_dokploy", lambda host: f"client@{host}")

    def fake_deploy_v2(**kw):
        rec.update(kw)
        target = make_target(
            kw["deploy_type"], service=kw["service"], version=SHA_CODE, iac_ref=SHA_IAC
        )
        return DeployV2Result(target, "staging", "deploy-primitive", {"sha": SHA_CODE})

    monkeypatch.setattr(dv2, "deploy_v2", fake_deploy_v2)
    return rec, json


def test_cli_passes_surface_through(cli, capsys, monkeypatch):
    monkeypatch.delenv("INTERNAL_DOMAIN", raising=False)
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
    # Dokploy control-plane host is the ONE shared zone (infra_domain()), never --domain
    # (the app's own routing domain) — "zp.io" here only ever flows through as rec["domain"].
    assert rec["client"] == "client@cloud.zitian.party"
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


def test_cli_dokploy_host_falls_back_to_the_known_org_domain_without_internal_domain(
    cli, monkeypatch
):
    # Local/manual runs that don't export INTERNAL_DOMAIN get the same known-good
    # literal every other INTERNAL_DOMAIN-reading call site falls back to
    # (infra_domain()) — never the caller's own --domain (that was the #550/#561 bug).
    rec, _json = cli
    monkeypatch.delenv("INTERNAL_DOMAIN", raising=False)
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
    assert rec["client"] == "client@cloud.zitian.party"


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

    import libs.deploy.dokploy_client as dk

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
    monkeypatch.delenv("INTERNAL_DOMAIN", raising=False)
    rec = {}

    def fake_down(kind, value, *, domain, client, service):
        rec.update(
            kind=kind, value=value, domain=domain, client=client, service=service
        )
        return _fake_down_result(kind, value, domain=domain, client=client)

    import libs.deploy.dokploy_client as dk

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
        "client": "client@cloud.zitian.party",
        "service": "finance_report/app",
    }
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "down" and out["alias"] == "pr-5"


def test_cli_down_rejects_a_fixed_env(monkeypatch, capsys):
    # staging/prod have no ephemeral alias to remove — --down must fail closed.
    import libs.deploy.dokploy_client as dk

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
    import libs.deploy.dokploy_client as dk

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

    # truealpha#860: the poll starts at 2 s and grows to 10 s; the attempt count is the
    # fewest polls whose pauses still cover the whole 120 s budget.
    assert (seen["interval"], seen["backoff"], seen["max_interval"]) == (
        2.0,
        1.25,
        10.0,
    )
    pauses = list(
        itertools.islice(
            status_poll_delays(seen["interval"], seen["backoff"], seen["max_interval"]),
            seen["attempts"],
        )
    )
    assert sum(pauses) >= 120 > sum(pauses[:-1])
    assert seen["attempts"] == 17  # a fixed 10 s interval needed 12 for the same budget


def test_every_runner_wait_uses_the_short_first_poll_schedule(calls, monkeypatch):
    """The secret supply is the short operation the schedule is for: it finished 7.1 s
    after its trigger on 2026-09-16 and was seen at the 10 s poll (truealpha#860)."""
    order = _runner_fakes(monkeypatch)
    _deploy(
        deploy_type="staging",
        version_ref="v1.2.3",
        iac_runner_url="https://iac.example",
        iac_webhook_secret="s",
    )
    polls = [kw for kind, kw in order if kind == "poll"]
    assert polls and all(
        (kw["interval"], kw["backoff"], kw["max_interval"]) == (2.0, 1.25, 10.0)
        and kw["attempts"] == dv2._poll_attempts_for_timeout(600)
        for kw in polls
    )


def test_status_poll_schedule_reaches_a_short_verdict_sooner_and_keeps_the_long_rate():
    pauses = list(itertools.islice(status_poll_delays(2.0, 1.25, 10.0), 12))
    polled_at = list(itertools.accumulate(pauses))
    # a 7.1 s secret supply is seen at the third pause (7.6 s), not at 10 s
    assert next(t for t in polled_at if t >= 7.1) < 8
    # past ~40 s the pause is the old 10 s: a long sync polls exactly as often as before
    assert pauses[8:] == [10.0] * 4
    assert status_poll_attempts(600, initial=10, backoff=1, maximum=10) == 60
    assert status_poll_attempts(600) - 60 <= 5


def test_deploy_phases_report_their_duration_on_stderr(calls, monkeypatch, capsys):
    _runner_fakes(monkeypatch)
    _deploy(
        deploy_type="staging",
        version_ref="v1.2.3",
        iac_runner_url="https://iac.example",
        iac_webhook_secret="s",
    )
    err = capsys.readouterr().err
    assert (
        "deploy_v2 progress: secret supply for finance_report/app in staging: completed after"
        in err
    )
    assert (
        "deploy_v2 progress: Dokploy promote of finance_report/app to staging:" in err
    )
    # never mistaken for the verdict line or the runner record reconcile parses
    assert "deploy_v2 failed:" not in err
    assert "ended '" not in err


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


# --- per-service domain override (truealpha#474's deploy_v2 head: the workflow's
# default --domain zitian.party leaked into INTERNAL_DOMAIN/APP_HOST for an app
# that owns its dedicated domain, so a v1.1.45 staging deploy still served on
# truealpha-staging.zitian.party while the canonical truealpha.club host 404'd.
# #586 fixed the App-repo request path only; deploy_v2 must apply the same
# domain_for_service override) --------------------------------------------------


def test_fixed_deploy_uses_the_services_own_domain_over_the_caller_input(calls):
    _deploy(service="truealpha/app", deploy_type="staging", version_ref="v0.0.10")
    assert calls["fixed"] is not None
    assert calls["fixed"]["domain"] == "truealpha.club"


def test_fixed_deploy_keeps_the_shared_domain_for_services_without_an_override(calls):
    _deploy(deploy_type="staging", version_ref="v0.0.10")
    assert calls["fixed"] is not None
    assert calls["fixed"]["domain"] == "zitian.party"


def test_platform_forwards_only_an_app_release_as_version_ref(monkeypatch):
    """truealpha#712: a digest-pinned platform service (truealpha/data_engine) pins the
    app release named by version_ref. The reconcile pins both axes to the infra2 tag
    (version_ref == iac_ref), which is never an app release and must not reach the
    deployer — v1.1.59's staging reconcile would have asked the registry for
    truealpha-data-engine:v1.1.59. `main` (deploy.yml's default) is not forwarded either."""
    sent, _res = _platform(monkeypatch, version_ref="v0.0.47")
    assert sent["version_ref"] == "v0.0.47"
    sent, _res = _platform(monkeypatch, version_ref="main")
    assert sent.get("version_ref") is None
    # the helper pins iac_ref to v0.0.0: the same tag as version_ref is the reconcile's shape
    sent, _res = _platform(monkeypatch, version_ref="v0.0.0")
    assert sent.get("version_ref") is None


# --- app-stack secret supply through the runner before a Dokploy promote (#649) --------


def _runner_fakes(monkeypatch, *, final_status="completed"):
    order: list = []

    def fake_trigger(**kw):
        order.append(("trigger", kw))
        return {"status": "accepted", "deployment_id": "d" * 16}

    def fake_poll(**kw):
        order.append(("poll", kw))
        return {"status": final_status, "deployment_id": "d" * 16}

    monkeypatch.setattr(dv2, "trigger_platform_deploy", fake_trigger)
    monkeypatch.setattr(dv2, "poll_platform_deploy_status", fake_poll)
    return order


def test_fixed_app_deploy_runs_the_secret_supply_through_the_runner_first(
    calls, monkeypatch
):
    order = _runner_fakes(monkeypatch)
    original_fixed = dv2._deploy_fixed

    def fixed_after_supply(env, code, **kw):
        order.append(("fixed", env))
        return original_fixed(env, code, **kw)

    monkeypatch.setattr(dv2, "_deploy_fixed", fixed_after_supply)
    result = _deploy(
        deploy_type="staging",
        version_ref="v1.2.3",
        iac_runner_url="https://iac.example",
        iac_webhook_secret="s",
    )
    kinds = [k for k, _ in order]
    assert kinds == ["trigger", "poll", "fixed"]
    trigger_kw = order[0][1]
    assert trigger_kw["action"] == "secrets-supply"
    assert trigger_kw["services"] == ["finance_report/app"]
    assert trigger_kw["env"] == "staging"
    assert order[1][1]["action"] == "secrets-supply"
    assert result.detail["secret_supply"]["status"] == "completed"


def test_fixed_app_deploy_fails_closed_when_the_supply_fails(calls, monkeypatch):
    _runner_fakes(monkeypatch, final_status="failed")
    with pytest.raises(RuntimeError, match="secret supply for finance_report/app"):
        _deploy(
            deploy_type="staging",
            version_ref="v1.2.3",
            iac_runner_url="https://iac.example",
            iac_webhook_secret="s",
        )
    assert calls["fixed"] is None  # never promoted


def test_fixed_app_deploy_without_runner_credentials_skips_the_supply(
    calls, monkeypatch
):
    order = _runner_fakes(monkeypatch)
    monkeypatch.delenv("IAC_RUNNER_URL", raising=False)
    monkeypatch.delenv("IAC_WEBHOOK_SECRET", raising=False)
    result = _deploy(deploy_type="staging", version_ref="v1.2.3")
    assert order == []
    assert result.detail["secret_supply"]["status"] == "skipped"
    assert calls["fixed"] is not None


# --- a lost runner request: staging re-submits once, production fails at once (#666) ----


_LOST_MESSAGE = (
    "iac_runner answered not_found for 100s while polling deploy dddddddddddd to staging "
    "(deployment {id}): the runner has no record of this deployment; it was probably "
    "recreated between the request and the first poll (#666)"
)
_RUNNER_SITES = ["platform", "secret-supply", "batch"]


def _lost(deployment_id="unknown"):
    return RunnerLostDeploymentError(_LOST_MESSAGE.format(id=deployment_id))


def _scripted_runner(monkeypatch, polls):
    """Fake runner: each trigger gets a fresh id (``...01``, ``...02``); each poll plays the
    next scripted outcome (an exception to raise, or a status dict to return)."""
    rec = {"triggers": [], "polls": []}

    def fake_trigger(**kw):
        rec["triggers"].append(kw)
        # a runaway re-submit loop must fail the test, not hang it
        assert len(rec["triggers"]) <= 4, "runaway re-submit loop"
        return {"status": "accepted", "deployment_id": f"{len(rec['triggers']):016x}"}

    def fake_poll(**kw):
        rec["polls"].append(kw)
        assert len(rec["polls"]) <= len(polls), (
            f"unexpected poll {len(rec['polls'])}: the script holds {len(polls)} outcome(s)"
        )
        outcome = polls[len(rec["polls"]) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(dv2, "trigger_platform_deploy", fake_trigger)
    monkeypatch.setattr(dv2, "poll_platform_deploy_status", fake_poll)
    return rec


def _run_runner_site(site, env_type):
    """Enter deploy_v2 at one of the three places that talk to the runner.

    platform       -> _deploy_platform  (one iac-pinned service)
    secret-supply  -> _supply_app_secrets  (the supply before an app stack's promote)
    batch          -> _deploy_platform_batch  (``--service a,b``, the reconcile's shape)
    """
    prod = env_type == "prod"
    if site == "platform":
        return _deploy(
            service="platform/redis",
            deploy_type=env_type,
            version_ref="",
            code_reviewed=True,
        )
    if site == "secret-supply":
        return _deploy(
            deploy_type=env_type,
            version_ref="v1.2.3",
            iac_runner_url="https://iac.example",
            iac_webhook_secret="s",
            staging_validated=prod,
            code_reviewed=True,
        )
    assert site == "batch"
    return dv2._deploy_platform_batch(
        ["platform/redis", "platform/alerting"],
        env_type,
        "v0.0.0",
        runner_url="https://iac.example",
        secret="s",
        triggered_by="deploy_v2",
        code_reviewed=True,
        wait=True,
        timeout=120,
    )


@pytest.mark.parametrize("site", _RUNNER_SITES)
def test_staging_resubmits_a_lost_request_once_and_succeeds(
    calls, monkeypatch, capsys, site
):
    rec = _scripted_runner(
        monkeypatch, [_lost(), {"status": "completed", "deployment_id": "x"}]
    )
    _run_runner_site(site, "staging")
    assert len(rec["triggers"]) == 2
    assert len(rec["polls"]) == 2
    # the second poll follows the second request's id, not the lost one
    assert rec["polls"][0]["deployment_id"] == f"{1:016x}"
    assert rec["polls"][1]["deployment_id"] == f"{2:016x}"
    err = capsys.readouterr().err
    assert err.count("lost; re-submitting once") == 1
    assert "deploy_v2 failed:" not in err and "ended '" not in err


@pytest.mark.parametrize("site", _RUNNER_SITES)
def test_staging_fails_when_the_resubmitted_request_is_lost_too(
    calls, monkeypatch, site
):
    # the script has more losses than the code may consume: a third request would find one
    rec = _scripted_runner(monkeypatch, [_lost(f"{n:016x}") for n in range(1, 5)])
    with pytest.raises(RunnerLostDeploymentError) as excinfo:
        _run_runner_site(site, "staging")
    assert len(rec["triggers"]) == 2  # exactly one re-submit, never a third request
    assert len(rec["polls"]) == 2
    message = str(excinfo.value)
    assert f"{1:016x}" in message and f"{2:016x}" in message
    assert "no record of this deployment" in message  # the cause survives
    assert isinstance(excinfo.value.__cause__, RunnerLostDeploymentError)


@pytest.mark.parametrize("site", _RUNNER_SITES)
def test_production_fails_at_once_on_a_lost_request(calls, monkeypatch, capsys, site):
    original = _lost(f"{1:016x}")
    # later losses stand ready: a production re-submit would find one and the test would see it
    rec = _scripted_runner(
        monkeypatch, [original] + [_lost(f"{n:016x}") for n in range(2, 5)]
    )
    with pytest.raises(RunnerLostDeploymentError) as excinfo:
        _run_runner_site(site, "prod")
    assert len(rec["triggers"]) == 1
    assert len(rec["polls"]) == 1
    assert excinfo.value is original  # the original error, not a re-wrapped one
    assert "re-submitting" not in capsys.readouterr().err


@pytest.mark.parametrize("site", _RUNNER_SITES)
@pytest.mark.parametrize(
    ("outcome", "raised", "match"),
    [
        (
            TimeoutError("did not settle within 17 polls"),
            TimeoutError,
            "did not settle",
        ),
        # a gateway failure is a RuntimeError too, but not a lost request: the runner may
        # still hold the deploy, so a second request could run beside it
        (
            RuntimeError("iac_runner unreachable for 200s while polling deploy"),
            RuntimeError,
            "unreachable",
        ),
        ({"status": "failed", "details": "boom"}, RuntimeError, "ended 'failed'"),
    ],
    ids=["timeout", "gateway-down", "failed-status"],
)
def test_staging_does_not_resubmit_any_other_failure(
    calls, monkeypatch, site, outcome, raised, match
):
    rec = _scripted_runner(monkeypatch, [outcome])
    with pytest.raises(raised, match=match) as excinfo:
        _run_runner_site(site, "staging")
    assert not isinstance(excinfo.value, RunnerLostDeploymentError)
    assert len(rec["triggers"]) == 1
    assert len(rec["polls"]) == 1


class _RunnerAnswer:
    def __init__(self, payload, status_code):
        self._payload = payload
        self.status_code = status_code
        self.content = b"x"

    def json(self):
        return self._payload

    def raise_for_status(self):
        assert self.status_code < 400, f"unexpected HTTP {self.status_code}"


@pytest.mark.parametrize("site", ["secret-supply", "batch"])
@pytest.mark.parametrize(
    ("env_type", "resubmits"), [("staging", True), ("prod", False)]
)
def test_the_real_client_loss_error_drives_the_resubmit_decision(
    calls, monkeypatch, site, env_type, resubmits
):
    """Join the two halves: the REAL poll function, on a runner that answers ``not_found``
    to the first request, must make deploy_v2 re-submit (staging) or stop (production)."""
    from libs import iac_runner_client

    real_poll = iac_runner_client.poll_platform_deploy_status
    moment = [1700000000.0]
    sent = []

    def fake_trigger(**kw):
        sent.append(kw)
        assert len(sent) <= 4, "runaway re-submit loop"
        return {"status": "accepted", "deployment_id": f"{len(sent):016x}"}

    def runner_transport(url, *, content, headers, timeout):
        # the runner of request 1 was recreated and forgot it; request 2 is a fresh runner's
        if len(sent) == 1:
            return _RunnerAnswer({"status": "not_found"}, 404)
        return _RunnerAnswer({"status": "completed"}, 200)

    def sleep(seconds):
        moment[0] += seconds

    monkeypatch.setattr(dv2, "trigger_platform_deploy", fake_trigger)
    monkeypatch.setattr(
        dv2,
        "poll_platform_deploy_status",
        lambda **kw: real_poll(
            **kw, now=lambda: moment[0], sleep=sleep, transport=runner_transport
        ),
    )
    if resubmits:
        _run_runner_site(site, env_type)
        assert len(sent) == 2
    else:
        with pytest.raises(RunnerLostDeploymentError, match="no record of this"):
            _run_runner_site(site, env_type)
        assert len(sent) == 1


def test_a_request_that_is_not_awaited_is_sent_once_and_never_polled(monkeypatch):
    rec = _scripted_runner(monkeypatch, [])
    response, final = dv2._trigger_and_poll(
        env="staging",
        ref=SHA_IAC,
        services=["platform/redis"],
        url="https://iac.example",
        secret="s",
        triggered_by="deploy_v2",
        wait=False,
        timeout=120,
        started=0.0,
    )
    assert response["deployment_id"] == f"{1:016x}" and final is None
    assert len(rec["triggers"]) == 1 and rec["polls"] == []


def test_the_runner_is_called_only_from_the_shared_trigger_and_poll_function():
    """A fourth call site would carry no lost-request rule. Walk the module's syntax tree
    (not its text): every call to the two client functions must sit inside
    ``_trigger_and_poll``."""
    client_functions = {"trigger_platform_deploy", "poll_platform_deploy_status"}
    tree = ast.parse(Path(dv2.__file__).read_text())
    callers: dict[str, set[str]] = {}
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in client_functions
            ):
                callers.setdefault(node.func.id, set()).add(function.name)
    # not green-while-empty: both functions are called, and only by the shared one
    assert callers == {name: {"_trigger_and_poll"} for name in client_functions}


def test_image_manifest_exists_success(monkeypatch):
    import libs.deploy.preflight as preflight

    monkeypatch.setattr(
        preflight,
        "resolve_image_digest",
        lambda *, image, reference, registry: "sha256:1234567890abcdef",
    )
    assert preflight._image_manifest_exists("ghcr.io/org/repo", "v1.0.0") is True


def test_image_manifest_exists_not_found(monkeypatch):
    from infra2_sdk.release import ReleaseError
    import libs.deploy.preflight as preflight

    def fake_resolve(*, image, reference, registry):
        raise ReleaseError(
            f"image {image}:{reference} does not exist in the registry (status 404)"
        )

    monkeypatch.setattr(preflight, "resolve_image_digest", fake_resolve)
    assert preflight._image_manifest_exists("ghcr.io/org/repo", "v1.0.0") is False


def test_image_manifest_exists_error_raises_runtime_error(monkeypatch):
    from infra2_sdk.release import ReleaseError
    import libs.deploy.preflight as preflight

    def fake_resolve(*, image, reference, registry):
        raise ReleaseError("network timeout or auth failure")

    monkeypatch.setattr(preflight, "resolve_image_digest", fake_resolve)
    with pytest.raises(RuntimeError, match="registry refused manifest check"):
        preflight._image_manifest_exists("ghcr.io/org/repo", "v1.0.0")


def test_image_manifest_exists_os_error_raises_runtime_error(monkeypatch):
    import libs.deploy.preflight as preflight

    def fake_resolve(*, image, reference, registry):
        raise OSError("connection reset by peer")

    monkeypatch.setattr(preflight, "resolve_image_digest", fake_resolve)
    with pytest.raises(RuntimeError, match="registry connection failed"):
        preflight._image_manifest_exists("ghcr.io/org/repo", "v1.0.0")
