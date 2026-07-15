"""Fail-closed tests for the cross-repository app deploy receiver."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from infra2_sdk.deploy import DeployOperation, DeployType
from infra2_sdk.refs import ResolvedRef

from libs import app_deploy_request as receiver
from tools import app_deploy_request as receiver_cli

SHA = "a" * 40
APP_REPO = "wangzitian0/finance_report"


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
            receiver.parse_request(payload(service="truealpha/app")),
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
    production = payload(
        deploy_type="prod",
        evidence={
            "source_run_url": f"https://github.com/{APP_REPO}/actions/runs/100",
            "source_run_id": "100",
            "staging_run_url": f"https://github.com/{APP_REPO}/actions/runs/101",
            "reviewed_change_url": f"https://github.com/{APP_REPO}/pull/10",
        },
    )
    receiver.validate_request_authority(
        receiver.parse_request(production),
        sender="wangzitian0",
        allow_production=True,
        resolve_image=resolved,
    )
    production["evidence"]["reviewed_change_url"] = (
        "https://github.com/wangzitian0/truealpha/pull/10"
    )
    with pytest.raises(ValueError, match="reviewed_change_url"):
        receiver.validate_request_authority(
            receiver.parse_request(production),
            sender="wangzitian0",
            allow_production=True,
            resolve_image=resolved,
        )


def test_production_request_is_disabled_until_remote_evidence_is_verified() -> None:
    production = payload(
        deploy_type="prod",
        evidence={
            "source_run_url": f"https://github.com/{APP_REPO}/actions/runs/100",
            "source_run_id": "100",
            "staging_run_url": f"https://github.com/{APP_REPO}/actions/runs/101",
            "reviewed_change_url": f"https://github.com/{APP_REPO}/pull/10",
        },
    )
    with pytest.raises(ValueError, match="production app deploy requests are disabled"):
        receiver.validate_request_authority(
            receiver.parse_request(production),
            sender="wangzitian0",
            resolve_image=resolved,
        )


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

    production_payload = payload(
        deploy_type="prod",
        evidence={
            "source_run_url": f"https://github.com/{APP_REPO}/actions/runs/100",
            "source_run_id": "100",
            "staging_run_url": f"https://github.com/{APP_REPO}/actions/runs/101",
            "reviewed_change_url": f"https://github.com/{APP_REPO}/pull/10",
        },
    )
    production = receiver.make_plan(
        production_payload,
        sender="wangzitian0",
        domain="zitian.party",
        timeout=600,
        repo_root=tmp_path,
        allow_production=True,
        resolve_image=resolved,
        runner=tags,
    )
    assert production.deploy_v2_args()[-2:] == [
        "--staging-validated",
        "--code-reviewed",
    ]


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


def test_cli_cannot_enable_production_requests(monkeypatch, capsys, tmp_path) -> None:
    request = payload(
        deploy_type="prod",
        evidence={
            "source_run_url": f"https://github.com/{APP_REPO}/actions/runs/100",
            "source_run_id": "100",
            "staging_run_url": f"https://github.com/{APP_REPO}/actions/runs/101",
            "reviewed_change_url": f"https://github.com/{APP_REPO}/pull/10",
        },
    )
    monkeypatch.setenv("APP_DEPLOY_REQUEST_JSON", json.dumps(request))

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
    assert "production app deploy requests are disabled" in capsys.readouterr().err
