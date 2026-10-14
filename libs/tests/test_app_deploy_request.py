"""Fail-closed tests for the cross-repository app deploy receiver."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from infra2_sdk.deploy import DeployOperation, DeployType
from infra2_sdk.refs import ResolvedRef

from libs.deploy import app_deploy_request as receiver
from tools import app_deploy_request as receiver_cli

SHA = "a" * 40
APP_REPO = "wangzitian0/finance_report"

# Mirrors the REAL contract each app checked into its own repo (#576):
# finance_report tools/production_evidence_policy.json (PR #1978) and
# truealpha's (PR #465) — the receiver fetches these at source_sha instead of
# consulting the deleted PRODUCTION_EVIDENCE_POLICIES dict.
FINANCE_REPORT_POLICY = {
    "contract_version": 1,
    "service": "finance_report/app",
    "source": {
        "workflow_path": ".github/workflows/deploy.yml",
        "event": "push",
        "display_title_template": "Release Images {version_ref}",
    },
    "staging": {
        "workflow_path": ".github/workflows/deploy.yml",
        "event": "workflow_dispatch",
        "display_title_template": "Deploy Staging {version_ref}",
    },
    "review_base_ref": "main",
}
TRUEALPHA_POLICY = {
    "contract_version": 1,
    "service": "truealpha/app",
    "source": {
        "workflow_path": ".github/workflows/ci-required.yml",
        "event": "push",
        "display_title_template": "Release Images {version_ref}",
    },
    "staging": {
        "workflow_path": ".github/workflows/deploy-release.yml",
        "event": "workflow_dispatch",
        "display_title_template": "Deploy staging {version_ref}",
        "require_head_sha": False,
    },
    "review_base_ref": "main",
}


def policy_path(repo: str = APP_REPO, sha: str = SHA) -> str:
    return f"/repos/{repo}/contents/tools/production_evidence_policy.json?ref={sha}"


def policy_contents(policy: dict) -> dict:
    # The GitHub contents API returns the file body base64-encoded.
    return {
        "content": base64.b64encode(json.dumps(policy).encode()).decode(),
        "encoding": "base64",
    }


def payload(**overrides) -> dict:
    data = {
        "contract_version": 1,
        "request_id": "run-12345678",
        "operation": "deploy",
        "service": "finance_report/app",
        "deploy_type": "staging",
        "version_ref": "v1.2.3",
        "source_repository": APP_REPO,
        "source_sha": SHA,
        "evidence": {
            "source_run_url": f"https://github.com/{APP_REPO}/actions/runs/100",
            "source_run_id": "100",
            "staging_run_url": "",
            "reviewed_change_url": "",
        },
    }
    data.update(overrides)
    return data


def production_payload(**overrides) -> dict:
    data = payload(
        deploy_type="prod",
        evidence={
            "source_run_url": f"https://github.com/{APP_REPO}/actions/runs/100",
            "source_run_id": "100",
            "staging_run_url": f"https://github.com/{APP_REPO}/actions/runs/101",
            "reviewed_change_url": f"https://github.com/{APP_REPO}/pull/10",
        },
    )
    data.update(overrides)
    return data


def successful_run(run_id: int) -> dict:
    run = {
        "status": "completed",
        "conclusion": "success",
        "head_sha": SHA,
        "html_url": f"https://github.com/{APP_REPO}/actions/runs/{run_id}",
        "repository": {"full_name": APP_REPO},
    }
    if run_id == 100:
        run.update(
            {
                "event": "push",
                "head_branch": "v1.2.3",
                "path": ".github/workflows/deploy.yml",
                "display_title": "Release Images v1.2.3",
            }
        )
    else:
        run.update(
            {
                "event": "workflow_dispatch",
                "head_branch": "main",
                "path": ".github/workflows/deploy.yml",
                "display_title": "Deploy Staging v1.2.3",
            }
        )
    return run


def merged_pull() -> dict:
    return {
        "state": "closed",
        "merged_at": "2026-07-15T00:00:00Z",
        "merge_commit_sha": SHA,
        "html_url": f"https://github.com/{APP_REPO}/pull/10",
        "base": {"ref": "main", "repo": {"full_name": APP_REPO}},
        "user": {"login": "octocat-author"},
    }


def approved_reviews() -> list[dict]:
    return [
        {
            "id": 1,
            "user": {"login": "octocat-reviewer"},
            "state": "APPROVED",
        }
    ]


def resolved(*args, **kwargs) -> ResolvedRef:
    return ResolvedRef(sha=SHA, image_ref="v1.2.3", form="tag")


def tags(args, **kwargs):
    if "production/v*.*.*" in args:
        return SimpleNamespace(stdout="production/v1.1.74\n")
    return SimpleNamespace(stdout="v1.1.76\nv1.1.75\n")


def test_parse_request_rejects_non_object_and_bad_json() -> None:
    with pytest.raises(ValueError, match="valid JSON"):
        receiver.parse_request("{")
    with pytest.raises(ValueError, match="JSON object"):
        receiver.parse_request("[]")


def test_authority_accepts_matching_sender_repo_and_ref() -> None:
    request = receiver.parse_request(payload())
    receiver.validate_request_authority(
        request,
        sender="wangzitian0",
        resolve_image=resolved,
    )


def test_authority_rejects_sender_service_and_repository() -> None:
    request = receiver.parse_request(payload())
    with pytest.raises(ValueError, match="sender"):
        receiver.validate_request_authority(
            request, sender="someone", resolve_image=resolved
        )
    with pytest.raises(ValueError, match="not enabled"):
        receiver.validate_request_authority(
            receiver.parse_request(payload(service="unregistered_app/app")),
            sender="wangzitian0",
            resolve_image=resolved,
        )
    with pytest.raises(ValueError, match="requires source_repository"):
        receiver.validate_request_authority(
            receiver.parse_request(payload(source_repository="wangzitian0/truealpha")),
            sender="wangzitian0",
            resolve_image=resolved,
        )


def test_authority_rejects_forged_evidence_and_sha() -> None:
    bad_evidence = payload(
        evidence={
            "source_run_url": "https://example.com/actions/runs/100",
            "source_run_id": "100",
            "staging_run_url": "",
            "reviewed_change_url": "",
        }
    )
    with pytest.raises(ValueError, match="source_run_url"):
        receiver.validate_request_authority(
            receiver.parse_request(bad_evidence),
            sender="wangzitian0",
            resolve_image=resolved,
        )

    def other_sha(*args, **kwargs):
        return ResolvedRef(sha="b" * 40, image_ref="v1.2.3", form="tag")

    with pytest.raises(ValueError, match="not source_sha"):
        receiver.validate_request_authority(
            receiver.parse_request(payload()),
            sender="wangzitian0",
            resolve_image=other_sha,
        )


def test_preview_pr_uses_pull_ref_resolution() -> None:
    calls = []

    def pull(number, *, repo):
        calls.append((number, repo))
        return ResolvedRef(sha=SHA, image_ref=SHA[:7], form="pr")

    request = receiver.parse_request(
        payload(deploy_type="preview/pr", version_ref="42")
    )
    receiver.validate_request_authority(
        request,
        sender="wangzitian0",
        resolve_pull=pull,
    )
    assert calls == [("42", "https://github.com/wangzitian0/finance_report.git")]


def test_remove_skips_remote_ref_resolution() -> None:
    def must_not_resolve(*args, **kwargs):
        raise AssertionError("remove should not resolve a deleted preview ref")

    request = receiver.parse_request(
        payload(operation="remove", deploy_type="preview/pr", version_ref="42")
    )
    receiver.validate_request_authority(
        request,
        sender="wangzitian0",
        resolve_pull=must_not_resolve,
    )


def test_production_requires_repo_scoped_staging_and_review_urls() -> None:
    production = production_payload()
    receiver.validate_request_authority(
        receiver.parse_request(production),
        sender="wangzitian0",
        production_evidence_verifier=lambda request: None,
        resolve_image=resolved,
    )
    production["evidence"]["reviewed_change_url"] = (
        "https://github.com/wangzitian0/truealpha/pull/10"
    )
    with pytest.raises(ValueError, match="reviewed_change_url"):
        receiver.validate_request_authority(
            receiver.parse_request(production),
            sender="wangzitian0",
            production_evidence_verifier=lambda request: None,
            resolve_image=resolved,
        )


def test_production_evidence_is_verified_from_github() -> None:
    responses = {
        policy_path(): policy_contents(FINANCE_REPORT_POLICY),
        f"/repos/{APP_REPO}/actions/runs/100": successful_run(100),
        f"/repos/{APP_REPO}/actions/runs/101": successful_run(101),
        f"/repos/{APP_REPO}/pulls/10": merged_pull(),
        f"/repos/{APP_REPO}/pulls/10/reviews": approved_reviews(),
    }
    calls = []

    def fetch(path: str) -> dict:
        calls.append(path)
        return responses[path]

    receiver.verify_production_evidence(
        receiver.parse_request(production_payload()),
        fetch_json=fetch,
    )

    # The app's own contract file is fetched FIRST (at source_sha), then the runs.
    assert calls == list(responses)


def test_missing_contract_file_fails_closed_naming_app_and_path() -> None:
    # #576 AC: an app without a checked-in contract is explicitly, loudly
    # staging-only — no silent fallback dict.
    def fetch(path: str) -> dict:
        raise ValueError(f"GitHub evidence request failed for {path}: HTTP 404")

    with pytest.raises(ValueError) as exc:
        receiver.verify_production_evidence(
            receiver.parse_request(production_payload()),
            fetch_json=fetch,
        )
    message = str(exc.value)
    assert "'finance_report/app' has no Production evidence contract" in message
    assert f"{APP_REPO}:tools/production_evidence_policy.json@{SHA}" in message


@pytest.mark.parametrize(
    "contents,error",
    [
        ({"content": 42}, "did not return file content"),
        (
            {"content": base64.b64encode(b"not json").decode()},
            "not valid JSON",
        ),
        (
            policy_contents({"contract_version": 1, "service": "finance_report/app"}),
            "is not a valid evidence contract",
        ),
        (
            policy_contents(TRUEALPHA_POLICY),
            "declares service 'truealpha/app', not 'finance_report/app'",
        ),
    ],
)
def test_malformed_or_misbound_contract_fails_closed(contents, error) -> None:
    responses = {policy_path(): contents}
    with pytest.raises(ValueError, match=error):
        receiver.verify_production_evidence(
            receiver.parse_request(production_payload()),
            fetch_json=responses.__getitem__,
        )


def test_truealpha_policy_verifies_its_real_run_shapes() -> None:
    # Shapes mirror the REAL captured runs (truealpha PR #465's fixtures):
    # a tag-push ci-required build, and a deploy-release staging dispatch whose
    # head_sha is main's tip at dispatch time — NOT the tag commit — which the
    # contract declares via require_head_sha=false (the 6th mismatch beyond
    # infra2#571's five, found while capturing the fixture).
    ta_repo = "wangzitian0/truealpha"
    request = receiver.parse_request(
        production_payload(
            service="truealpha/app",
            source_repository=ta_repo,
            evidence={
                "source_run_url": f"https://github.com/{ta_repo}/actions/runs/100",
                "source_run_id": "100",
                "staging_run_url": f"https://github.com/{ta_repo}/actions/runs/101",
                "reviewed_change_url": f"https://github.com/{ta_repo}/pull/10",
            },
        )
    )
    source_run = {
        "status": "completed",
        "conclusion": "success",
        "head_sha": SHA,
        "html_url": f"https://github.com/{ta_repo}/actions/runs/100",
        "repository": {"full_name": ta_repo},
        "event": "push",
        "head_branch": "v1.2.3",
        "path": ".github/workflows/ci-required.yml",
        "display_title": "Release Images v1.2.3",
    }
    staging_run = {
        "status": "completed",
        "conclusion": "success",
        "head_sha": "c" * 40,  # main's tip at dispatch time, not the tag commit
        "html_url": f"https://github.com/{ta_repo}/actions/runs/101",
        "repository": {"full_name": ta_repo},
        "event": "workflow_dispatch",
        "head_branch": "main",
        "path": ".github/workflows/deploy-release.yml",
        "display_title": "Deploy staging v1.2.3",
    }
    pull = {
        "state": "closed",
        "merged_at": "2026-07-15T00:00:00Z",
        "merge_commit_sha": SHA,
        "html_url": f"https://github.com/{ta_repo}/pull/10",
        "base": {"ref": "main", "repo": {"full_name": ta_repo}},
    }
    responses = {
        policy_path(ta_repo): policy_contents(TRUEALPHA_POLICY),
        f"/repos/{ta_repo}/actions/runs/100": source_run,
        f"/repos/{ta_repo}/actions/runs/101": staging_run,
        f"/repos/{ta_repo}/pulls/10": pull,
        f"/repos/{ta_repo}/pulls/10/reviews": approved_reviews(),
    }

    receiver.verify_production_evidence(request, fetch_json=responses.__getitem__)

    # require_head_sha stays enforced where declared: the SOURCE run's head_sha
    # still fails closed on a mismatch even though staging's is exempt.
    responses[f"/repos/{ta_repo}/actions/runs/100"] = {
        **source_run,
        "head_sha": "d" * 40,
    }
    with pytest.raises(ValueError, match="source run.*head_sha"):
        receiver.verify_production_evidence(request, fetch_json=responses.__getitem__)


@pytest.mark.parametrize(
    "path,replacement,error",
    [
        ("actions/runs/100", {"conclusion": "failure"}, "source run.*success"),
        ("actions/runs/101", {"head_sha": "b" * 40}, "staging run.*head_sha"),
        (
            "actions/runs/100",
            {"repository": {"full_name": "wangzitian0/truealpha"}},
            "source run.*repository",
        ),
        (
            "actions/runs/100",
            {"path": ".github/workflows/infra-ci.yml"},
            "source run.*workflow",
        ),
        (
            "actions/runs/101",
            {"event": "push"},
            "staging run.*event",
        ),
        ("pulls/10", {"merged_at": None}, "reviewed pull request.*merged"),
        (
            "pulls/10",
            {"merge_commit_sha": "b" * 40},
            "reviewed pull request.*merge_commit_sha",
        ),
        (
            "pulls/10",
            {"base": {"ref": "release", "repo": {"full_name": APP_REPO}}},
            "reviewed pull request.*base",
        ),
    ],
)
def test_production_evidence_rejects_untrusted_remote_state(
    path, replacement, error
) -> None:
    responses = {
        policy_path(): policy_contents(FINANCE_REPORT_POLICY),
        f"/repos/{APP_REPO}/actions/runs/100": successful_run(100),
        f"/repos/{APP_REPO}/actions/runs/101": successful_run(101),
        f"/repos/{APP_REPO}/pulls/10": merged_pull(),
        f"/repos/{APP_REPO}/pulls/10/reviews": approved_reviews(),
    }
    response_path = next(key for key in responses if path in key)
    responses[response_path] = {**responses[response_path], **replacement}

    with pytest.raises(ValueError, match=error):
        receiver.verify_production_evidence(
            receiver.parse_request(production_payload()),
            fetch_json=responses.__getitem__,
        )


@pytest.mark.parametrize(
    "reviews,error",
    [
        (
            [{"user": {"login": "reviewer"}, "state": "CHANGES_REQUESTED"}],
            "pending CHANGES_REQUESTED",
        ),
        (
            [
                {"user": {"login": "reviewer"}, "state": "APPROVED"},
                {"user": {"login": "reviewer2"}, "state": "CHANGES_REQUESTED"},
            ],
            "pending CHANGES_REQUESTED",
        ),
    ],
)
def test_production_evidence_rejects_pending_changes_requested(reviews, error) -> None:
    responses = {
        policy_path(): policy_contents(FINANCE_REPORT_POLICY),
        f"/repos/{APP_REPO}/actions/runs/100": successful_run(100),
        f"/repos/{APP_REPO}/actions/runs/101": successful_run(101),
        f"/repos/{APP_REPO}/pulls/10": merged_pull(),
        f"/repos/{APP_REPO}/pulls/10/reviews": reviews,
    }
    with pytest.raises(ValueError, match=error):
        receiver.verify_production_evidence(
            receiver.parse_request(production_payload()),
            fetch_json=responses.__getitem__,
        )


@pytest.mark.parametrize(
    "reviews",
    [
        [],
        [{"user": {"login": "octocat-author"}, "state": "APPROVED"}],
        [{"user": {"login": "reviewer"}, "state": "COMMENTED"}],
        [{"user": {"login": "reviewer"}, "state": "APPROVED"}],
        [
            {"user": {"login": "reviewer"}, "state": "CHANGES_REQUESTED"},
            {"user": {"login": "reviewer"}, "state": "APPROVED"},
        ],
    ],
)
def test_production_evidence_accepts_reviews_without_changes_requested(reviews) -> None:
    responses = {
        policy_path(): policy_contents(FINANCE_REPORT_POLICY),
        f"/repos/{APP_REPO}/actions/runs/100": successful_run(100),
        f"/repos/{APP_REPO}/actions/runs/101": successful_run(101),
        f"/repos/{APP_REPO}/pulls/10": merged_pull(),
        f"/repos/{APP_REPO}/pulls/10/reviews": reviews,
    }
    receiver.verify_production_evidence(
        receiver.parse_request(production_payload()),
        fetch_json=responses.__getitem__,
    )


@pytest.mark.parametrize(
    "field,url",
    [
        (
            "source_run_url",
            f"https://github.com/{APP_REPO}/actions/runs/100/attempts/2",
        ),
        (
            "staging_run_url",
            f"https://github.com/{APP_REPO}/actions/runs/101?check_suite_focus=true",
        ),
        ("reviewed_change_url", f"https://github.com/{APP_REPO}/pull/10/files"),
    ],
)
def test_production_evidence_requires_canonical_urls(field, url) -> None:
    request_payload = production_payload()
    request_payload["evidence"][field] = url

    with pytest.raises(ValueError, match=field):
        receiver.verify_production_evidence(
            receiver.parse_request(request_payload),
            fetch_json=lambda path: {},
        )


def test_iac_ref_distinguishes_staging_candidate_from_production_target(
    tmp_path,
) -> None:
    assert (
        receiver.select_iac_ref(DeployType.STAGING, repo_root=tmp_path, runner=tags)
        == "v1.1.76"
    )
    assert (
        receiver.select_iac_ref(DeployType.PREVIEW_PR, repo_root=tmp_path, runner=tags)
        == "main"
    )
    assert (
        receiver.select_iac_ref(DeployType.PRODUCTION, repo_root=tmp_path, runner=tags)
        == "v1.1.74"
    )

    with pytest.raises(ValueError, match="invalid production marker"):
        receiver.select_iac_ref(
            DeployType.PRODUCTION,
            repo_root=tmp_path,
            runner=lambda *args, **kwargs: SimpleNamespace(stdout="not-a-release\n"),
        )


def test_iac_ref_fails_closed_without_production_marker(tmp_path) -> None:
    def no_marker(args, **_kwargs):
        if "production/v*.*.*" in args:
            return SimpleNamespace(stdout="")
        return SimpleNamespace(stdout="v1.1.76\n")

    with pytest.raises(ValueError, match="production IaC state is unknown"):
        receiver.select_iac_ref(
            DeployType.PRODUCTION, repo_root=tmp_path, runner=no_marker
        )


def test_iac_ref_fails_closed_on_malformed_production_marker(tmp_path) -> None:
    with pytest.raises(ValueError, match="invalid production marker"):
        receiver.select_iac_ref(
            DeployType.PRODUCTION,
            repo_root=tmp_path,
            runner=lambda *_args, **_kwargs: SimpleNamespace(
                stdout="production/v1.1.53-rc.1\n"
            ),
        )


def test_iac_ref_refuses_a_marker_that_predates_the_release_pin(tmp_path) -> None:
    """#650: a stale marker deploys the data engine from a deployer that cannot honour
    the release pin, and today the release only finds out after prod is recreated."""

    def stale_marker(args, **_kwargs):
        if "production/v*.*.*" in args:
            return SimpleNamespace(stdout="production/v1.1.52\n")
        return SimpleNamespace(stdout="v1.1.76\n")

    with pytest.raises(ValueError, match="predates v1.1.59"):
        receiver.select_iac_ref(
            DeployType.PRODUCTION, repo_root=tmp_path, runner=stale_marker
        )

    def just_new_enough(args, **_kwargs):
        if "production/v*.*.*" in args:
            return SimpleNamespace(
                stdout=f"production/{receiver.MINIMUM_PRODUCTION_MARKER}\n"
            )
        return SimpleNamespace(stdout="v1.1.76\n")

    assert (
        receiver.select_iac_ref(
            DeployType.PRODUCTION, repo_root=tmp_path, runner=just_new_enough
        )
        == receiver.MINIMUM_PRODUCTION_MARKER
    )


def test_plan_names_the_newest_release_next_to_the_ref_it_runs_at(tmp_path) -> None:
    """The promotion lag is on screen in the plan, before anything is deployed."""
    production = receiver.make_plan(
        production_payload(),
        sender="wangzitian0",
        domain="zitian.party",
        timeout=600,
        repo_root=tmp_path,
        production_evidence_verifier=lambda request: None,
        resolve_image=resolved,
        runner=tags,
    )
    assert production.iac_ref == "v1.1.74"
    assert production.newest_iac_tag == "v1.1.76"
    assert production.to_dict()["newest_iac_tag"] == "v1.1.76"

    preview = receiver.make_plan(
        payload(deploy_type="preview/pr", version_ref="42"),
        sender="wangzitian0",
        domain="zitian.party",
        timeout=600,
        repo_root=tmp_path,
        resolve_image=resolved,
        resolve_pull=lambda number, *, repo: ResolvedRef(
            sha=SHA, image_ref=SHA[:7], form="pr"
        ),
        runner=tags,
    )
    assert preview.iac_ref == "main"
    assert preview.newest_iac_tag == ""


def test_marker_status_reports_the_lag_and_calls_a_pre_pin_marker_stale(
    tmp_path,
) -> None:
    """The daily #650 report: both coordinates, the gap, and a verdict."""
    current = receiver.marker_status(repo_root=tmp_path, runner=tags)
    assert (current.production_marker, current.newest_tag) == ("v1.1.74", "v1.1.76")
    assert current.releases_behind == 2
    assert not current.stale
    assert current.line() == (
        "production marker v1.1.74 (2 release(s) behind); newest release v1.1.76"
    )

    def stale(args, **_kwargs):
        if "production/v*.*.*" in args:
            return SimpleNamespace(stdout="production/v1.1.52\n")
        return SimpleNamespace(stdout="v1.1.76\nv1.1.52\n")

    rotted = receiver.marker_status(repo_root=tmp_path, runner=stale)
    assert rotted.stale
    # The line names the release the marker is behind (#632) and what to do about it
    # (#650), because it is what a paging ops check prints (review on this PR).
    assert "STALE" in rotted.line()
    assert "#632" in rotted.line() and "#650" in rotted.line()

    def caught_up(args, **_kwargs):
        return SimpleNamespace(
            stdout=(
                "production/v1.1.76\n" if "production/v*.*.*" in args else "v1.1.76\n"
            )
        )

    assert receiver.marker_status(repo_root=tmp_path, runner=caught_up).line() == (
        "production marker v1.1.76 (current); newest release v1.1.76"
    )


def test_plan_builds_staging_and_production_deploy_v2_args(tmp_path) -> None:
    staging = receiver.make_plan(
        payload(),
        sender="wangzitian0",
        domain="zitian.party",
        timeout=600,
        repo_root=tmp_path,
        resolve_image=resolved,
        runner=tags,
    )
    assert staging.iac_ref == "v1.1.76"
    assert staging.deploy_v2_args()[-2:] == ["--expected-sha", SHA]

    production = receiver.make_plan(
        production_payload(),
        sender="wangzitian0",
        domain="zitian.party",
        timeout=600,
        repo_root=tmp_path,
        production_evidence_verifier=lambda request: None,
        resolve_image=resolved,
        runner=tags,
    )
    assert production.deploy_v2_args()[-2:] == [
        "--staging-validated",
        "--code-reviewed",
    ]


def test_plan_domain_defaults_to_the_passed_in_shared_domain(tmp_path) -> None:
    """finance_report has no Deployer.domain override — the caller-supplied
    INTERNAL_DOMAIN (today's behavior for every service before truealpha) flows
    through unchanged."""
    plan = receiver.make_plan(
        payload(),
        sender="wangzitian0",
        domain="zitian.party",
        timeout=600,
        repo_root=tmp_path,
        resolve_image=resolved,
        runner=tags,
    )
    assert plan.domain == "zitian.party"


def test_plan_domain_is_overridden_by_the_service_registry(tmp_path) -> None:
    """truealpha/app declares its own domain (Deployer.domain = "truealpha.club") —
    the plan must use it even when a different shared domain is passed in, so
    truealpha never silently lands on the platform's shared zitian.party."""
    truealpha_repo = "wangzitian0/truealpha"
    plan = receiver.make_plan(
        payload(
            service="truealpha/app",
            source_repository=truealpha_repo,
            evidence={
                "source_run_url": f"https://github.com/{truealpha_repo}/actions/runs/100",
                "source_run_id": "100",
                "staging_run_url": "",
                "reviewed_change_url": "",
            },
        ),
        sender="wangzitian0",
        domain="zitian.party",
        timeout=600,
        repo_root=tmp_path,
        resolve_image=resolved,
        runner=tags,
    )
    assert plan.domain == "truealpha.club"
    assert "--domain" in plan.deploy_v2_args()
    assert (
        plan.deploy_v2_args()[plan.deploy_v2_args().index("--domain") + 1]
        == "truealpha.club"
    )


def test_remove_plan_uses_down_without_expected_sha(tmp_path) -> None:
    plan = receiver.make_plan(
        payload(operation="remove", deploy_type="preview/pr", version_ref="42"),
        sender="wangzitian0",
        domain="zitian.party",
        timeout=600,
        repo_root=tmp_path,
        runner=tags,
    )
    assert plan.request.operation == DeployOperation.REMOVE
    assert plan.iac_ref == "main"
    assert "--down" in plan.deploy_v2_args()
    assert "--expected-sha" not in plan.deploy_v2_args()


@pytest.mark.parametrize(
    "overrides,expected",
    [
        # truealpha#860: staging proves its own coordinate; production keeps the canary
        ({}, False),  # staging (payload()'s default deploy_type)
        ({"operation": "rollback"}, False),
        ({"deploy_type": "prod"}, True),
        ({"deploy_type": "prod", "operation": "rollback"}, True),
        ({"deploy_type": "preview/pr", "version_ref": "42"}, False),
        ({"deploy_type": "preview/branch", "version_ref": "main"}, False),
        (
            {"operation": "remove", "deploy_type": "preview/pr", "version_ref": "42"},
            False,
        ),
    ],
)
def test_requires_preflight_canary_is_the_single_source_the_workflow_reads(
    tmp_path, overrides, expected
) -> None:
    """app-deploy-request.yml's preflight_canary pre-filter is pinned to this property
    by test_app_deploy_request_workflow.py and re-checked against it at run time. A
    production request additionally needs staging + review evidence, so it goes through
    production_payload()."""
    build_payload = (
        production_payload if overrides.get("deploy_type") == "prod" else payload
    )
    plan = receiver.make_plan(
        build_payload(**overrides),
        sender="wangzitian0",
        domain="zitian.party",
        timeout=600,
        repo_root=tmp_path,
        production_evidence_verifier=lambda request: None,
        resolve_image=resolved,
        resolve_pull=lambda *_a, **_k: ResolvedRef(sha=SHA, image_ref="42", form="pr"),
        runner=tags,
    )
    assert plan.requires_preflight_canary is expected
    assert plan.to_dict()["requires_preflight_canary"] is expected


@pytest.mark.parametrize(
    "domain,timeout", [("", 600), ("bad domain", 600), ("ok.test", 0)]
)
def test_plan_rejects_bad_execution_settings(tmp_path, domain, timeout) -> None:
    with pytest.raises(ValueError):
        receiver.make_plan(
            payload(),
            sender="wangzitian0",
            domain=domain,
            timeout=timeout,
            repo_root=tmp_path,
            resolve_image=resolved,
            runner=tags,
        )


def test_plan_cli_reads_payload_from_environment(monkeypatch, capsys, tmp_path) -> None:
    request = payload(operation="remove", deploy_type="preview/pr", version_ref="42")
    monkeypatch.setenv("APP_DEPLOY_REQUEST_JSON", json.dumps(request))
    result = receiver_cli.main(
        [
            "plan",
            "--sender",
            "wangzitian0",
            "--domain",
            "zitian.party",
            "--repo-root",
            str(tmp_path),
        ]
    )
    assert result == 0
    assert (
        json.loads(capsys.readouterr().out)["request"]["request_id"] == "run-12345678"
    )


def test_cli_rejects_iac_coordinate_change_after_validation(
    monkeypatch, capsys, tmp_path
) -> None:
    request = payload(operation="remove", deploy_type="preview/pr", version_ref="42")
    monkeypatch.setenv("APP_DEPLOY_REQUEST_JSON", json.dumps(request))

    result = receiver_cli.main(
        [
            "plan",
            "--sender",
            "wangzitian0",
            "--domain",
            "zitian.party",
            "--repo-root",
            str(tmp_path),
            "--expected-iac-ref",
            "v9.9.9",
        ]
    )

    assert result == 1
    assert "iac_ref changed" in capsys.readouterr().err


def test_cli_fails_closed_when_production_evidence_is_unavailable(
    monkeypatch, capsys, tmp_path
) -> None:
    monkeypatch.setenv("APP_DEPLOY_REQUEST_JSON", json.dumps(production_payload()))

    def unavailable(request) -> None:
        raise ValueError("remote verification unavailable")

    monkeypatch.setattr(receiver, "verify_production_evidence", unavailable)

    result = receiver_cli.main(
        [
            "execute",
            "--sender",
            "wangzitian0",
            "--domain",
            "zitian.party",
            "--repo-root",
            str(tmp_path),
        ]
    )

    assert result == 1
    assert "remote verification unavailable" in capsys.readouterr().err


def test_execute_promotes_the_declared_companions_after_the_primary(tmp_path) -> None:
    """truealpha#712: one app-deploy request promotes the app AND the data engine. The
    companion runs at the same version_ref / iac_ref / type, without the app-image
    --expected-sha assertion (it is a digest-pinned platform service; the runner pins the
    release's digest from version_ref), and a companion failure is the request's failure."""
    truealpha = "https://github.com/wangzitian0/truealpha"
    production = receiver.make_plan(
        production_payload(
            service="truealpha/app",
            source_repository="wangzitian0/truealpha",
            evidence={
                "source_run_url": f"{truealpha}/actions/runs/100",
                "source_run_id": "100",
                "staging_run_url": f"{truealpha}/actions/runs/101",
                "reviewed_change_url": f"{truealpha}/pull/10",
            },
        ),
        sender="wangzitian0",
        domain="zitian.party",
        timeout=600,
        repo_root=tmp_path,
        resolve_image=resolved,
        runner=tags,
        production_evidence_verifier=lambda request: None,
    )
    runs: list[list[str]] = []

    def run(args: list[str]) -> int:
        runs.append(list(args))
        return 0

    assert receiver_cli.execute_plan(production, run=run) == 0
    assert [a[a.index("--service") + 1] for a in runs] == [
        "truealpha/app",
        "truealpha/data_engine",
    ]
    primary, companion = runs
    assert "--expected-sha" in primary and "--expected-sha" not in companion
    for flag in ("--type", "--version-ref", "--iac-ref", "--domain"):
        assert companion[companion.index(flag) + 1] == primary[primary.index(flag) + 1]
    assert "--staging-validated" in companion and "--code-reviewed" in companion

    # the companion's exit code is the request's
    codes = iter([0, 7])
    assert receiver_cli.execute_plan(production, run=lambda args: next(codes)) == 7
    # a failed primary never reaches the companion
    seen: list[list[str]] = []

    def failing(args: list[str]) -> int:
        seen.append(list(args))
        return 3

    assert receiver_cli.execute_plan(production, run=failing) == 3
    assert len(seen) == 1


def _gate_cli(monkeypatch, request: dict, *extra: str) -> int:
    """The receiver CLI with every remote fact faked (no GitHub, no git)."""
    monkeypatch.setenv("APP_DEPLOY_REQUEST_JSON", json.dumps(request))
    from libs.deploy import app_deploy_request as shim

    make_plan = shim.make_plan

    def offline_plan(payload_json, **kwargs):
        return make_plan(
            payload_json,
            **kwargs,
            production_evidence_verifier=lambda request: None,
            resolve_image=resolved,
            runner=tags,
        )

    monkeypatch.setattr(shim, "make_plan", offline_plan)
    return receiver_cli.main(
        [
            "plan",
            "--sender",
            "wangzitian0",
            "--domain",
            "zitian.party",
            "--repo-root",
            ".",
            *extra,
        ]
    )


def test_canary_job_refuses_a_request_its_plan_does_not_canary(
    monkeypatch, capsys
) -> None:
    """truealpha#860: the workflow's pre-filter ran the canary job for a staging request
    (a drift) — the canary job's own validation stops before any credential is used."""
    result = _gate_cli(monkeypatch, payload(), "--require-preflight-canary")
    assert result == 1
    assert "pre-filter has drifted" in capsys.readouterr().err


def test_canary_job_accepts_a_production_request(monkeypatch, capsys) -> None:
    result = _gate_cli(monkeypatch, production_payload(), "--require-preflight-canary")
    assert result == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["requires_preflight_canary"] is True
    assert plan["iac_ref"] == "v1.1.74"


@pytest.mark.parametrize("canary_result", ["skipped", "failure", "cancelled", ""])
def test_production_never_executes_without_a_green_canary(
    monkeypatch, capsys, canary_result
) -> None:
    """The pre-filter skipped the canary for a request the plan says must be canaried:
    the deploy job refuses in its credential-free validation step."""
    result = _gate_cli(
        monkeypatch,
        production_payload(),
        "--preflight-canary-result",
        canary_result,
    )
    assert result == 1
    assert "requires a green preflight canary" in capsys.readouterr().err


@pytest.mark.parametrize(
    "request_payload,canary_result",
    [
        (payload(), "skipped"),
        (payload(operation="rollback"), "skipped"),
        (production_payload(), "success"),
    ],
)
def test_deploy_job_accepts_the_canary_result_its_plan_expects(
    monkeypatch, request_payload, canary_result
) -> None:
    assert (
        _gate_cli(
            monkeypatch,
            request_payload,
            "--preflight-canary-result",
            canary_result,
        )
        == 0
    )


def test_gate_is_not_checked_for_an_operator_run_without_the_flags(tmp_path) -> None:
    plan = receiver.make_plan(
        production_payload(),
        sender="wangzitian0",
        domain="zitian.party",
        timeout=600,
        repo_root=tmp_path,
        production_evidence_verifier=lambda request: None,
        resolve_image=resolved,
        runner=tags,
    )
    receiver_cli.check_preflight_canary_gate(plan)  # no workflow facts: no verdict
    with pytest.raises(ValueError, match="requires a green preflight canary"):
        receiver_cli.check_preflight_canary_gate(plan, canary_result="skipped")
    receiver_cli.check_preflight_canary_gate(
        plan, canary_job_ran=True, canary_result="success"
    )


# Request expiry (#1232). Facts from the GitHub API, read on 2026-10-13: receiver run
# 38344114882 was created at 03:57:12Z. Its sender (truealpha run 38344073277) stopped
# waiting at 04:33:30Z. The owner approved the deploy job at 05:53:07Z.
INCIDENT_CREATED_AT = "2026-10-13T03:57:12Z"
CREATED = datetime(2026, 10, 13, 3, 57, 12, tzinfo=timezone.utc)
INCIDENT_SENDER_STOPPED = datetime(2026, 10, 13, 4, 33, 30, tzinfo=timezone.utc)
INCIDENT_APPROVED = datetime(2026, 10, 13, 5, 53, 7, tzinfo=timezone.utc)
INCIDENT_REFUSAL = (
    "request expired: created 2026-10-13T03:57:12Z, now 2026-10-13T05:53:07Z, "
    "limit 1440 s. A sender may stop waiting before this deploy ends, and nothing "
    "would verify the deploy. Dispatch the release again."
)


def _age(
    request_payload: dict,
    *,
    now: datetime,
    created_at: str = INCIDENT_CREATED_AT,
    run_attempt: str = "1",
) -> float | None:
    return receiver.check_request_age(
        receiver.parse_request(request_payload),
        created_at=created_at,
        run_attempt=run_attempt,
        now=now,
    )


@pytest.mark.parametrize("seconds", [0, 1, 1411, 1439, 1440])
def test_a_production_request_inside_the_limit_is_accepted(seconds) -> None:
    """The limit is inclusive: a request exactly at the limit still executes."""
    assert receiver.PRODUCTION_REQUEST_MAX_AGE_SECONDS == 24 * 60
    age = _age(production_payload(), now=CREATED + timedelta(seconds=seconds))
    assert age == seconds


def test_a_production_request_one_second_past_the_limit_is_refused() -> None:
    limit = receiver.PRODUCTION_REQUEST_MAX_AGE_SECONDS
    with pytest.raises(ValueError, match=r"^request expired: .* limit 1440 s\. "):
        _age(production_payload(), now=CREATED + timedelta(seconds=limit + 1))


def test_the_refusal_names_both_times_the_limit_and_the_remedy() -> None:
    with pytest.raises(ValueError) as refused:
        _age(production_payload(), now=INCIDENT_APPROVED)
    assert str(refused.value) == INCIDENT_REFUSAL


def test_a_negative_request_age_is_refused_as_a_clock_error() -> None:
    """The deploy job starts after the canary job, so a real age is never negative.
    A negative age means that the runner clock or the creation time is wrong."""
    with pytest.raises(
        ValueError,
        match=r"^request age is negative: created 2026-10-13T03:57:12Z, "
        r"now 2026-10-13T03:57:11Z\. ",
    ):
        _age(production_payload(), now=CREATED - timedelta(seconds=1))


def test_the_limit_refuses_the_incident_request_when_its_sender_stopped() -> None:
    """Receiver run 38344114882 was 2178 s old when its sender stopped waiting."""
    with pytest.raises(ValueError, match="^request expired: "):
        _age(production_payload(), now=INCIDENT_SENDER_STOPPED)


def test_the_limit_accepts_the_oldest_observed_legitimate_approval() -> None:
    """Receiver run 37776291342 printed its plan, where the check runs, 1411 s after
    its creation. Its sender confirmed the deploy (2026-10-08). It passes with 29 s
    to spare."""
    checked = datetime(2026, 10, 8, 12, 45, 5, tzinfo=timezone.utc)
    age = _age(production_payload(), created_at="2026-10-08T12:21:34Z", now=checked)
    assert age == 1411


@pytest.mark.parametrize("operation", ["deploy", "rollback"])
def test_every_production_operation_expires(operation) -> None:
    """The SDK limits remove to preview and canary targets, so these are all of them."""
    with pytest.raises(ValueError, match="^request expired: "):
        _age(production_payload(operation=operation), now=INCIDENT_APPROVED)


@pytest.mark.parametrize(
    "created_at", [INCIDENT_CREATED_AT, "2026-10-13T03:57:12+00:00"]
)
def test_a_staging_request_has_no_age_limit(created_at) -> None:
    """A staging request can wait in the queue (#1232 scope: production only)."""
    later = INCIDENT_APPROVED + timedelta(days=1)
    assert _age(payload(), created_at=created_at, now=later) is None


@pytest.mark.parametrize(
    "request_payload",
    [payload(), payload(deploy_type="preview/pr", version_ref="42")],
    ids=["staging", "preview"],
)
@pytest.mark.parametrize(
    "created_at", ["", "null", "not a time", "2026-10-13T03:57:12"]
)
def test_every_request_needs_a_valid_creation_time(request_payload, created_at) -> None:
    """Every request parses the time, so staging deploys use the read and the parse
    before the first production deploy does. Only production has the limits."""
    with pytest.raises(ValueError, match="^request creation time "):
        _age(request_payload, created_at=created_at, now=INCIDENT_APPROVED)


@pytest.mark.parametrize(
    "created_at",
    ["", "null", "2026-10-13", "2026-10-13T03:57:12", "13 Oct 2026 03:57:12 UTC"],
)
def test_a_production_request_without_a_valid_creation_time_is_refused(
    created_at,
) -> None:
    """An empty, unparsable or zoneless time cannot give an age: fail closed."""
    with pytest.raises(ValueError, match="^request creation time "):
        _age(production_payload(), created_at=created_at, now=INCIDENT_APPROVED)


def _expiry_cli(monkeypatch, request: dict, *extra: str, now: datetime) -> int:
    monkeypatch.setattr(receiver_cli, "_utc_now", lambda: now)
    return _gate_cli(monkeypatch, request, *extra)


def test_the_deploy_job_refuses_an_expired_request_before_the_iac_check(
    monkeypatch, capsys
) -> None:
    """The #1232 replay. A newer production marker landed while the run waited, so the
    iac_ref check also fails. The age check runs first and names the real reason."""
    result = _expiry_cli(
        monkeypatch,
        production_payload(),
        "--request-created-at",
        INCIDENT_CREATED_AT,
        "--run-attempt",
        "1",
        "--expected-iac-ref",
        "v1.2.30",
        "--preflight-canary-result",
        "success",
        now=INCIDENT_APPROVED,
    )
    err = capsys.readouterr().err
    assert result == 1
    assert f"app deploy request failed: {INCIDENT_REFUSAL}\n" in err
    assert "iac_ref changed" not in err


def test_the_deploy_job_executes_a_production_request_inside_the_window(
    monkeypatch, capsys
) -> None:
    result = _expiry_cli(
        monkeypatch,
        production_payload(),
        "--request-created-at",
        INCIDENT_CREATED_AT,
        "--run-attempt",
        "1",
        "--expected-iac-ref",
        "v1.1.74",
        "--preflight-canary-result",
        "success",
        now=CREATED + timedelta(seconds=1411),
    )
    captured = capsys.readouterr()
    assert result == 0, captured.err
    assert json.loads(captured.out)["iac_ref"] == "v1.1.74"
    assert "request age 1411 s, limit 1440 s" in captured.err


@pytest.mark.parametrize("run_attempt", ["1", "2"])
def test_the_deploy_job_does_not_age_a_staging_request(
    monkeypatch, capsys, run_attempt
) -> None:
    """No age limit and no re-run rule for staging: it can wait in the queue."""
    result = _expiry_cli(
        monkeypatch,
        payload(),
        "--request-created-at",
        INCIDENT_CREATED_AT,
        "--run-attempt",
        run_attempt,
        "--preflight-canary-result",
        "skipped",
        now=INCIDENT_APPROVED + timedelta(days=1),
    )
    captured = capsys.readouterr()
    assert result == 0, captured.err
    assert "request age" not in captured.err


def test_the_deploy_job_refuses_a_production_request_without_its_creation_time(
    monkeypatch, capsys
) -> None:
    """The workflow gives an empty value when it did not read the time."""
    result = _expiry_cli(
        monkeypatch,
        production_payload(),
        "--request-created-at",
        "",
        "--run-attempt",
        "1",
        "--preflight-canary-result",
        "success",
        now=INCIDENT_APPROVED,
    )
    assert result == 1
    assert "app deploy request failed: request creation time ''" in (
        capsys.readouterr().err
    )


# Re-run rule (#1232). The sender stops when the first attempt of its receiver run ends
# (infra2_sdk.deploy.dispatch_and_wait raises on the first conclusion that is not
# success). A re-run keeps created_at, so the age alone does not stop it.
RE_RUN_REFUSAL = (
    "request is a re-run (attempt {attempt}). The sender does not wait for a re-run, "
    "and nothing would verify the deploy. Dispatch the release again."
)


@pytest.mark.parametrize("attempt", ["2", "3", "10"])
def test_a_production_re_run_is_refused_even_when_it_is_young(attempt) -> None:
    """Job 2 failed, then the owner re-ran it and approved it at 1250 s."""
    with pytest.raises(ValueError) as refused:
        _age(
            production_payload(),
            now=CREATED + timedelta(seconds=1250),
            run_attempt=attempt,
        )
    assert str(refused.value) == RE_RUN_REFUSAL.format(attempt=attempt)


def test_the_re_run_rule_comes_before_the_age_limit() -> None:
    with pytest.raises(ValueError, match=r"^request is a re-run \(attempt 2\)\. "):
        _age(production_payload(), now=INCIDENT_APPROVED, run_attempt="2")


@pytest.mark.parametrize("attempt", ["", "0", "-1", "1.0", "one", " 1", "\uff11"])
def test_a_production_request_without_a_valid_run_attempt_is_refused(attempt) -> None:
    """GITHUB_RUN_ATTEMPT is a positive ASCII integer. Anything else fails closed."""
    with pytest.raises(ValueError, match="^run attempt "):
        _age(
            production_payload(),
            now=CREATED + timedelta(seconds=10),
            run_attempt=attempt,
        )


@pytest.mark.parametrize("attempt", ["2", "", "x"])
def test_a_staging_request_has_no_re_run_rule(attempt) -> None:
    assert _age(
        payload(), now=CREATED + timedelta(seconds=10), run_attempt=attempt
    ) is (None)


def test_the_deploy_job_refuses_a_re_run_before_the_iac_check(
    monkeypatch, capsys
) -> None:
    result = _expiry_cli(
        monkeypatch,
        production_payload(),
        "--request-created-at",
        INCIDENT_CREATED_AT,
        "--run-attempt",
        "2",
        "--expected-iac-ref",
        "v1.2.30",
        "--preflight-canary-result",
        "success",
        now=CREATED + timedelta(seconds=600),
    )
    err = capsys.readouterr().err
    assert result == 1
    assert f"app deploy request failed: {RE_RUN_REFUSAL.format(attempt=2)}\n" in err
    assert "iac_ref changed" not in err


def test_the_deploy_job_refuses_a_production_request_without_its_run_attempt(
    monkeypatch, capsys
) -> None:
    """The two flags go together. One of them alone fails closed."""
    result = _expiry_cli(
        monkeypatch,
        production_payload(),
        "--request-created-at",
        INCIDENT_CREATED_AT,
        "--preflight-canary-result",
        "success",
        now=CREATED + timedelta(seconds=600),
    )
    assert result == 1
    assert "app deploy request failed: run attempt ''" in capsys.readouterr().err


def test_a_run_attempt_without_a_creation_time_is_refused(monkeypatch, capsys) -> None:
    result = _expiry_cli(
        monkeypatch,
        production_payload(),
        "--run-attempt",
        "1",
        "--preflight-canary-result",
        "success",
        now=CREATED + timedelta(seconds=600),
    )
    assert result == 1
    assert "app deploy request failed: request creation time ''" in (
        capsys.readouterr().err
    )


def test_the_deploy_job_refuses_a_staging_request_without_its_creation_time(
    monkeypatch, capsys
) -> None:
    """A staging deploy runs the same read and parse, so a broken read shows on
    staging first."""
    result = _expiry_cli(
        monkeypatch,
        payload(),
        "--request-created-at",
        "",
        "--run-attempt",
        "1",
        "--preflight-canary-result",
        "skipped",
        now=INCIDENT_APPROVED,
    )
    assert result == 1
    assert "app deploy request failed: request creation time ''" in (
        capsys.readouterr().err
    )


def test_authority_uses_the_sdk_evidence_url_validator() -> None:
    """The receiver has no local copy of the URL check (infra2-sdk#105)."""
    assert not hasattr(receiver, "_validate_evidence_urls")
    assert not hasattr(receiver, "_require_github_path")
    foreign_repo_url = payload(
        evidence={
            "source_run_url": "https://github.com/other-owner/other-repo/actions/runs/100",
            "source_run_id": "100",
            "staging_run_url": "",
            "reviewed_change_url": "",
        }
    )
    with pytest.raises(ValueError, match="source_run_url"):
        receiver.validate_request_authority(
            receiver.parse_request(foreign_repo_url),
            sender="wangzitian0",
            resolve_image=resolved,
        )
