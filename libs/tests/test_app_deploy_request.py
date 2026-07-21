"""Fail-closed tests for the cross-repository app deploy receiver."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from infra2_sdk.deploy import DeployOperation, DeployType
from infra2_sdk.refs import ResolvedRef

from libs import app_deploy_request as receiver
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
    }


def resolved(*args, **kwargs) -> ResolvedRef:
    return ResolvedRef(sha=SHA, image_ref="v1.2.3", form="tag")


def tags(*args, **kwargs):
    return SimpleNamespace(stdout="v1.1.28\nv1.1.27\n")


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
            "is not valid JSON",
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
    }

    receiver.verify_production_evidence(request, fetch_json=responses.__getitem__)

    # require_head_sha stays enforced where declared: the SOURCE run's head_sha
    # still fails closed on a mismatch even though staging's is exempt.
    responses[f"/repos/{ta_repo}/actions/runs/100"] = {
        **source_run,
        "head_sha": "d" * 40,
    }
    with pytest.raises(ValueError, match="source run.*head_sha"):
        receiver.verify_production_evidence(
            request, fetch_json=responses.__getitem__
        )


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
    }
    response_path = next(key for key in responses if path in key)
    responses[response_path] = {**responses[response_path], **replacement}

    with pytest.raises(ValueError, match=error):
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


def test_github_evidence_fetch_uses_read_only_api_token(monkeypatch) -> None:
    calls = []
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(
            200,
            json={"status": "completed"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(receiver.httpx, "get", get)

    assert receiver._fetch_github_json("/repos/example/app/actions/runs/1") == {
        "status": "completed"
    }
    url, kwargs = calls[0]
    assert url == "https://api.github.com/repos/example/app/actions/runs/1"
    assert kwargs["headers"]["Authorization"] == "Bearer test-token"
    assert kwargs["follow_redirects"] is False


@pytest.mark.parametrize(
    "response,error",
    [
        (httpx.Response(403, text="secret response"), "HTTP 403"),
        (httpx.Response(200, content=b"{"), "JSONDecodeError"),
        (httpx.Response(200, json=[]), "must be an object"),
    ],
)
def test_github_evidence_fetch_fails_closed_without_leaking_response(
    monkeypatch, response, error
) -> None:
    def get(url, **kwargs):
        response.request = httpx.Request("GET", url)
        return response

    monkeypatch.setattr(receiver.httpx, "get", get)

    with pytest.raises(ValueError, match=error) as exc_info:
        receiver._fetch_github_json("/repos/example/app/actions/runs/1")
    assert "secret response" not in str(exc_info.value)


def test_iac_ref_is_latest_merged_release_for_fixed_envs(tmp_path) -> None:
    assert (
        receiver.select_iac_ref(DeployType.STAGING, repo_root=tmp_path, runner=tags)
        == "v1.1.28"
    )
    assert (
        receiver.select_iac_ref(DeployType.PREVIEW_PR, repo_root=tmp_path, runner=tags)
        == "main"
    )

    with pytest.raises(ValueError, match="no released infra2"):
        receiver.select_iac_ref(
            DeployType.PRODUCTION,
            repo_root=tmp_path,
            runner=lambda *args, **kwargs: SimpleNamespace(stdout="not-a-release\n"),
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
    assert staging.iac_ref == "v1.1.28"
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
    assert plan.deploy_v2_args()[plan.deploy_v2_args().index("--domain") + 1] == "truealpha.club"


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
