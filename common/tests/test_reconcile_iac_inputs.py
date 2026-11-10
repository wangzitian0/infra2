"""Tests for main-push input-drift reconcile of iac_pinned services."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

import tools.reconcile_iac_inputs as reconcile
from tools.reconcile_iac_inputs import (
    MANIFEST_PATH,
    assert_after_on_main,
    assert_production_marker_advances,
    build_deploy_commands,
    build_plan,
    commands_to_apply,
    is_zero_sha,
    main,
    production_marker_tag,
    record_production_marker,
    run_deploy_commands,
)

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "reconcile-iac-inputs.yml"
SHA = "a" * 40


def test_plan_selects_alerting_when_baked_tooling_changes() -> None:
    plan = build_plan(["tools/observability_roundtrip_probe.py"])

    assert plan.selected["platform/alerting"].startswith("declared dep")
    assert plan.staging_services == ["platform/alerting"]
    assert plan.prod_services == ["platform/alerting"]


def test_plan_ignores_app_fixed_deploy_without_version_ref() -> None:
    plan = build_plan(
        ["finance_report/finance_report/10.app/compose.yaml"],
        app_release_tags={},
    )

    assert "finance_report/app" in plan.ignored
    assert plan.services == []


def test_plan_selects_app_when_repos_yaml_or_app_dir_changes() -> None:
    plan = build_plan(["finance_report/finance_report/10.app/compose.yaml"])

    assert "finance_report/app" in plan.selected
    assert plan.app_release_tags["finance_report/app"].startswith("v")
    assert "finance_report/app" in plan.staging_services
    assert "finance_report/app" in plan.prod_services


def test_companion_service_promoted_once_after_app_and_excluded_from_platform_batch() -> (
    None
):
    plan = build_plan(
        [
            "platform/12.alerting/compose.yaml",
            "truealpha/truealpha/10.app/compose.yaml",
            "truealpha/truealpha/30.data_engine/compose.yaml",
        ],
        app_release_tags={
            "finance_report/app": "v0.2.46",
            "truealpha/app": "v0.2.77",
        },
    )
    commands = build_deploy_commands(
        plan,
        iac_ref="v1.2.3",
        domain="zitian.party",
        timeout=600,
        python_executable="python",
    )
    staging_cmds = [c for c in commands if c.deploy_type == "staging"]
    assert [c.service for c in staging_cmds] == [
        "platform/alerting",
        "truealpha/app",
        "truealpha/data_engine",
    ]
    assert (
        staging_cmds[1].argv[staging_cmds[1].argv.index("--version-ref") + 1]
        == "v0.2.77"
    )
    assert (
        staging_cmds[2].argv[staging_cmds[2].argv.index("--version-ref") + 1]
        == "v0.2.77"
    )
    assert staging_cmds[2].argv[staging_cmds[2].argv.index("--iac-ref") + 1] == "v1.2.3"


def test_changed_app_services_from_repos_yaml_detects_single_app_tag_bump() -> None:
    before_yaml = yaml.safe_dump(
        {
            "repos": [
                {
                    "id": "finance-report",
                    "role": "external-application",
                    "release_tag": "v0.2.46",
                },
                {
                    "id": "truealpha",
                    "role": "external-application",
                    "release_tag": "v0.2.76",
                },
            ]
        }
    )
    after_yaml = yaml.safe_dump(
        {
            "repos": [
                {
                    "id": "finance-report",
                    "role": "external-application",
                    "release_tag": "v0.2.46",
                },
                {
                    "id": "truealpha",
                    "role": "external-application",
                    "release_tag": "v0.2.77",
                },
            ]
        }
    )

    def runner(argv, **_kwargs):
        if "v1.0.0:harness/repos.yaml" in argv:
            return subprocess.CompletedProcess(argv, 0, before_yaml, "")
        if "v1.0.1:harness/repos.yaml" in argv:
            return subprocess.CompletedProcess(argv, 0, after_yaml, "")
        return subprocess.CompletedProcess(argv, 1, "", "missing")

    changed = reconcile.changed_app_services_from_repos_yaml(
        ROOT, before="v1.0.0", after="v1.0.1", runner=runner
    )
    assert list(changed) == ["truealpha/app"]
    assert "v0.2.76 -> v0.2.77" in changed["truealpha/app"]


def test_advance_app_release_train_computes_next_patch_tag() -> None:
    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 0, f"{SHA}\trefs/heads/main\n", "")

    info = reconcile.advance_app_release_train(
        "truealpha/app",
        repo_url="https://github.com/wangzitian0/truealpha.git",
        existing_tags=["v0.2.76", "v0.2.77"],
        candidate_ref="main",
        runner=runner,
    )
    assert info["candidate_sha"] == SHA
    assert info["image_ref"] == SHA[:7]
    assert info["next_tag"] == "v0.2.78"


def test_manifest_change_reconciles_all_iac_pinned_services() -> None:
    plan = build_plan([MANIFEST_PATH])

    assert "platform/alerting" in plan.services
    assert "finance_report/postgres" in plan.services
    assert "finance_report/app" not in plan.services
    assert "dependency manifest changed" in plan.selected["platform/alerting"]


def test_deployer_change_reconciles_all_iac_pinned_services() -> None:
    """Select all iac_pinned services when the shared source config hash formula changes."""
    plan = build_plan(["common/deploy/deployer.py"])

    assert set(plan.services) == set(reconcile.iac_pinned_services())
    assert (
        plan.selected["platform/postgres"]
        == "source config hash formula changed (common/deploy/deployer.py)"
    )


def test_prod_only_services_skip_staging() -> None:
    plan = build_plan(["platform/11.signoz/compose.yaml"])

    assert "platform/signoz" not in plan.staging_services
    assert plan.prod_services == ["platform/signoz"]


def test_not_yet_in_production_services_skip_prod() -> None:
    """#542 (the v1.1.34 2/14 prod-promote failure): an iac_pinned service whose
    Deployer declares `not_yet_in_production = True` must fan out to staging
    only, and be surfaced in the plan rather than silently dropped.

    The exclusion set is DERIVED from Deployer attrs, so this test is the flag's
    changelog: postgres graduated 2026-07-19, data_engine graduated 2026-07-27
    (owner-approved TOPT production scope) — the set is empty today, and the
    mechanism stays pinned so the next staging-scoped service re-populates it
    deliberately, in this test, in the same PR."""
    plan = build_plan([MANIFEST_PATH])  # selects every iac_pinned service

    for service in ("truealpha/data_engine", "truealpha/postgres"):
        assert service in plan.selected
        assert service in plan.staging_services
        assert service in plan.prod_services
        assert service not in plan.not_yet_in_production
    assert plan.not_yet_in_production == []
    # In-production services are untouched by the filter.
    assert "platform/alerting" in plan.prod_services
    assert plan.to_dict()["not_yet_in_production"] == plan.not_yet_in_production


def test_deploy_commands_use_deploy_v2_and_prod_review_signals() -> None:
    plan = build_plan(["platform/12.alerting/compose.yaml"])
    commands = build_deploy_commands(
        plan,
        iac_ref=SHA,
        domain="zitian.party",
        timeout=123,
        python_executable="python",
    )

    assert [command.deploy_type for command in commands] == ["staging", "prod"]
    assert commands[0].argv[:3] == ["python", "-m", "tools.deploy_v2"]
    assert (
        commands[0].argv[commands[0].argv.index("--service") + 1] == "platform/alerting"
    )
    assert "--code-reviewed" not in commands[0].argv
    assert "--code-reviewed" in commands[1].argv
    assert "--staging-validated" in commands[1].argv
    assert commands[1].argv[commands[1].argv.index("--iac-ref") + 1] == SHA


def test_deploy_commands_batch_services_by_environment() -> None:
    plan = build_plan(
        [
            "platform/12.alerting/compose.yaml",
            "platform/23.prefect/compose.yaml",
        ]
    )
    commands = build_deploy_commands(
        plan,
        iac_ref=SHA,
        domain="zitian.party",
        timeout=3600,
        python_executable="python",
    )

    assert [command.deploy_type for command in commands] == ["staging", "prod"]
    assert (
        commands[0].argv[commands[0].argv.index("--service") + 1]
        == "platform/alerting,platform/prefect"
    )
    assert commands[0].argv[commands[0].argv.index("--timeout") + 1] == "3600"


def test_run_deploy_commands_stops_on_first_failure() -> None:
    plan = build_plan(
        [
            "platform/12.alerting/compose.yaml",
            "platform/23.prefect/compose.yaml",
        ]
    )
    commands = build_deploy_commands(
        plan, iac_ref=SHA, domain="zitian.party", timeout=600
    )
    calls = []

    def fake_runner(argv, **_kwargs):
        calls.append(argv)
        if len(calls) == 2:
            return subprocess.CompletedProcess(argv, 1, "", "failed")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True}), "")

    results = run_deploy_commands(commands, runner=fake_runner)

    assert len(results) == 2
    assert results[-1]["returncode"] == 1
    assert len(calls) == 2


def test_zero_sha_detection() -> None:
    assert is_zero_sha("0" * 40)
    assert not is_zero_sha(SHA)


def _fake_git(*, on_main: bool, resolved: str = "b" * 40):
    """Fake git: ``rev-parse`` resolves a ref to a sha; ``merge-base --is-ancestor``
    returns 0 (reachable from origin/main) or 1 (off-main)."""

    def runner(argv, **_kwargs):
        if argv[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(argv, 0, resolved + "\n", "")
        if argv[:3] == ["git", "merge-base", "--is-ancestor"]:
            return subprocess.CompletedProcess(argv, 0 if on_main else 1, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    return runner


def test_guard_rejects_off_main_tag() -> None:
    # the v1.1.16 incident: a release tag cut on an unmerged feature branch must be
    # refused before any real staging/prod deploy. Enforces the Infra-011 invariant
    # "iac_pinned prod reconcile only from reviewed main" fail-closed.
    with pytest.raises(SystemExit, match="not reachable from"):
        assert_after_on_main("v1.1.16", ROOT, runner=_fake_git(on_main=False))


def test_guard_accepts_on_main_tag() -> None:
    # a tag reachable from origin/main promotes normally (no raise).
    assert_after_on_main("v1.1.17", ROOT, runner=_fake_git(on_main=True))


def _alerting_commands():
    plan = build_plan(["platform/12.alerting/compose.yaml"])
    return build_deploy_commands(plan, iac_ref=SHA, domain="zitian.party", timeout=600)


def test_default_applies_staging_only() -> None:
    # release decoupling: a tag push auto-applies staging (soak), never prod.
    applied = commands_to_apply(_alerting_commands(), dry_run=False, promote_prod=False)
    assert [c.deploy_type for c in applied] == ["staging"]


def test_promote_prod_applies_prod_only() -> None:
    # prod is a separate, explicit promotion step.
    applied = commands_to_apply(_alerting_commands(), dry_run=False, promote_prod=True)
    assert [c.deploy_type for c in applied] == ["prod"]


def test_production_marker_is_immutable_and_idempotent() -> None:
    calls = []

    def runner(argv, **_kwargs):
        calls.append(argv)
        if argv[:3] == ["git", "rev-parse", "--verify"]:
            return subprocess.CompletedProcess(argv, 0, "b" * 40 + "\n", "")
        if argv[:3] == ["git", "push", "origin"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected command: {argv}")

    marker = record_production_marker("v1.2.3", ROOT, runner=runner)

    assert marker == "production/v1.2.3"
    assert calls[-1] == ["git", "push", "origin", "refs/tags/production/v1.2.3"]


def test_production_marker_created_and_pushed_after_first_promotion() -> None:
    calls = []

    def runner(argv, **_kwargs):
        calls.append(argv)
        if argv[:3] == ["git", "rev-parse", "--verify"]:
            if argv[3] == "v1.2.3^{commit}":
                return subprocess.CompletedProcess(argv, 0, "b" * 40 + "\n", "")
            return subprocess.CompletedProcess(argv, 128, "", "missing")
        return subprocess.CompletedProcess(argv, 0, "", "")

    assert record_production_marker("v1.2.3", ROOT, runner=runner) == (
        "production/v1.2.3"
    )
    assert ["git", "tag", "production/v1.2.3", "b" * 40] in calls
    assert ["git", "push", "origin", "refs/tags/production/v1.2.3"] in calls


APP_REJECTION = (
    "remote: error: GH013: refusing to allow a GitHub App to create or update "
    "workflow `.github/workflows/app-deploy-request.yml` without `workflows` permission"
)


def _marker_runner(push_code: int, push_stderr: str, tag: str = "v1.2.3"):
    def runner(argv, **_kwargs):
        if argv[:3] == ["git", "rev-parse", "--verify"]:
            if argv[3] == f"{tag}^{{commit}}":
                return subprocess.CompletedProcess(argv, 0, "b" * 40 + "\n", "")
            return subprocess.CompletedProcess(argv, 128, "", "missing")
        if argv[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(argv, push_code, "", push_stderr + "\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    return runner


def test_production_marker_push_rejected_by_github_app_rule_raises_rejected() -> None:
    with pytest.raises(reconcile.ProductionMarkerRejected) as raised:
        record_production_marker(
            "v1.2.3", ROOT, runner=_marker_runner(1, APP_REJECTION)
        )

    assert str(raised.value) == (
        f"cannot push production marker production/v1.2.3: {APP_REJECTION}"
    )


@pytest.mark.parametrize("stderr", ["network down", "refusing to update this ref"])
def test_production_marker_other_push_failure_still_exits_with_error(stderr) -> None:
    with pytest.raises(SystemExit) as raised:
        record_production_marker("v1.2.3", ROOT, runner=_marker_runner(1, stderr))

    assert raised.value.code == (
        f"::error::cannot push production marker production/v1.2.3: {stderr}"
    )


def test_production_marker_rejection_text_on_a_successful_push_is_ignored() -> None:
    marker = record_production_marker(
        "v1.2.3", ROOT, runner=_marker_runner(0, APP_REJECTION)
    )

    assert marker == "production/v1.2.3"


def test_production_marker_rejects_non_release_ref() -> None:
    for invalid in ("main", "v1.2.3-rc.1"):
        with pytest.raises(ValueError, match="version tag"):
            production_marker_tag(invalid)


def test_production_marker_refuses_backward_move() -> None:
    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 0, "production/v1.2.3\n", "")

    with pytest.raises(SystemExit, match="cannot move backward"):
        assert_production_marker_advances("v1.2.2", ROOT, runner=runner)

    assert assert_production_marker_advances("v1.2.3", ROOT, runner=runner) == "v1.2.3"
    assert assert_production_marker_advances("v1.2.4", ROOT, runner=runner) == "v1.2.3"


def test_dry_run_applies_nothing() -> None:
    commands = _alerting_commands()
    assert commands_to_apply(commands, dry_run=True, promote_prod=False) == []
    assert commands_to_apply(commands, dry_run=True, promote_prod=True) == []


def test_main_rejects_invalid_prod_ref_before_provenance_or_deploy(
    monkeypatch,
) -> None:
    provenance_called = False

    def provenance(*_args, **_kwargs):
        nonlocal provenance_called
        provenance_called = True

    monkeypatch.setattr(reconcile, "assert_after_on_main", provenance)

    with pytest.raises(ValueError, match="version tag"):
        main(
            [
                "--after",
                "main",
                "--promote-prod",
                "--changed-file",
                "platform/12.alerting/compose.yaml",
            ]
        )

    assert provenance_called is False


@pytest.mark.parametrize("extra", [[], ["--dry-run"]])
def test_main_requires_first_production_baseline_before_provenance(
    monkeypatch,
    extra,
) -> None:
    provenance_called = False

    monkeypatch.setattr(
        reconcile, "assert_production_marker_advances", lambda *_args, **_kwargs: None
    )

    def provenance(*_args, **_kwargs):
        nonlocal provenance_called
        provenance_called = True

    monkeypatch.setattr(reconcile, "assert_after_on_main", provenance)

    with pytest.raises(SystemExit, match="explicit known-production --before"):
        main(
            [
                "--after",
                "v1.2.3",
                "--promote-prod",
                "--changed-file",
                "platform/12.alerting/compose.yaml",
                *extra,
            ]
        )

    assert provenance_called is False


def test_main_rejects_before_that_disagrees_with_current_marker(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        reconcile,
        "assert_production_marker_advances",
        lambda *_args, **_kwargs: "v1.2.2",
    )

    with pytest.raises(SystemExit, match="must match current marker"):
        main(
            [
                "--before",
                "v1.2.1",
                "--after",
                "v1.2.3",
                "--promote-prod",
                "--changed-file",
                "platform/12.alerting/compose.yaml",
            ]
        )


def test_main_dry_run_plans_without_deploying(capsys) -> None:
    # the PR shift-left gate: --dry-run builds the plan (fan-out resolves) but applies
    # nothing and skips the on-main guard, so a PR can preview release impact safely.
    rc = main(
        [
            "--after",
            "HEAD",
            "--dry-run",
            "--changed-file",
            "platform/12.alerting/compose.yaml",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["dry_run"] is True
    assert out["applied"] == []
    assert out["results"] == []
    assert "platform/alerting" in out["plan"]["services"]


def test_guard_rejects_unresolvable_ref() -> None:
    def runner(argv, **_kwargs):
        if argv[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(argv, 128, "", "unknown revision")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(SystemExit, match="cannot resolve"):
        assert_after_on_main("v9.9.9", ROOT, runner=runner)


def test_guard_distinguishes_unresolvable_base_from_off_main() -> None:
    # merge-base --is-ancestor exits 128 when the base ref (origin/main) is missing.
    # That must NOT be reported as "off-main" — it means the base was not fetched.
    def runner(argv, **_kwargs):
        if argv[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(argv, 0, "b" * 40 + "\n", "")
        if argv[:3] == ["git", "merge-base", "--is-ancestor"]:
            return subprocess.CompletedProcess(
                argv, 128, "", "fatal: Not a valid object name origin/main"
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(SystemExit, match="unresolvable"):
        assert_after_on_main("v1.1.17", ROOT, runner=runner)


def test_reconcile_workflow_contract() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    text = WORKFLOW.read_text(encoding="utf-8")

    # staging/prod deploy release tags only, so reconcile is tag-triggered (promote a tag),
    # NOT main-push triggered (which would promote a moving sha).
    assert workflow["on"]["push"]["tags"] == ["v*.*.*"]
    assert "branches" not in workflow["on"]["push"]
    assert workflow["jobs"]["reconcile"]["timeout-minutes"] == 120
    assert "python -m tools.reconcile_iac_inputs" in text
    assert "IAC_WEBHOOK_SECRET is required" in text
    assert "--timeout 3300" in text
    assert "fetch-depth: 0" in text
    assert "deploy_v2/iac_runner" in text
    # release decoupling: prod is an explicit promotion, never an automatic tag-push deploy.
    # promote_prod is the single workflow_dispatch input for staging vs prod (no duplicate target_env).
    assert "promote_prod" in workflow["on"]["workflow_dispatch"]["inputs"]
    assert "target_env" not in workflow["on"]["workflow_dispatch"]["inputs"]
    assert "--promote-prod" in text
    # the provenance guard relies on origin/main being resolvable in a tag-push checkout;
    # lock the exact fetch step so a future workflow edit can't silently break the guard.
    assert "refs/remotes/origin/main" in text
    assert workflow["permissions"]["contents"] == "write"
    assert "production/v* marker" in text.lower()
    assert 'production_before="${production_marker#production/}"' in text
    assert "first production marker requires an explicit known-production" in text
    assert 'before="$production_before"' in text


def test_reconcile_workflow_run_name_shows_the_release_tag() -> None:
    """#1488: a production run that waits for approval must show its tag in the run title."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    run_name = workflow.get("run-name")
    assert isinstance(run_name, str), "run-name is missing from the workflow"
    # Tag pushes and the 2-hour schedule have empty inputs, so the title falls back to the ref.
    assert run_name == (
        "Reconcile IaC Inputs "
        "(${{ inputs.promote_prod && 'PROD' || 'staging' }} "
        "${{ inputs.after || github.ref_name }})"
    )


def test_promote_prod_refuses_a_tag_without_a_green_staging_soak() -> None:
    """2026-09-07: v1.1.61 was promoted to production while its own staging reconcile had
    failed; production alerting crash-looped until the rollback. The guard step runs only
    on an explicit production promotion and needs a green push-triggered run of this
    workflow for the same tag — a dispatch run (dry run, or a promotion) does not count."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["reconcile"]["steps"]
    names = [step.get("name") for step in steps]
    guard = steps[
        names.index("Production promotion guard (soak, identity, change set)")
    ]
    assert names.index("Fetch origin/main for provenance guard") < names.index(
        guard["name"]
    )
    assert names.index(guard["name"]) < names.index("Reconcile changed IaC inputs")
    assert "inputs.promote_prod" in guard["if"] and "workflow_dispatch" in guard["if"]
    assert guard["env"]["GH_TOKEN"] == "${{ github.token }}"
    # gh run list needs actions: read on the workflow token (review on #641)
    # and Layer 2 GitHub OIDC requires id-token: write (#1126)
    assert workflow["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
        "issues": "write",
        "actions": "read",
        "id-token": "write",
    }
    script = guard["run"]
    # the soak lookup is the tested tool (common/tests/test_promotion_soak_guard.py), which
    # finds the run by commit and owns the "has no green staging soak" refusal (#970).
    assert 'python -m tools.promotion_soak_guard "$after"' in script
    assert "merge-base --is-ancestor" in script
    assert "production/v*.*.*" in script and "git diff --stat" in script


_RUNNER_FINAL_STDERR = (
    "deploy_v2 failed: platform batch deploy of ['platform/alerting', 'truealpha/data_engine'] "
    "to staging ended 'failed': {'deployment_id': 'abc', 'details': {'failure_summary': ["
    "{'error_kind': 'onepassword_auth_failed', 'next_action': 'OP_SERVICE_ACCOUNT_TOKEN is invalid. "
    "Recreate the service account.', 'service': 'platform/alerting', 'summary': 'record', "
    "'task': 'alerting.sync'}, {'error_kind': 'unknown_invoke_failure', 'next_action': 'Inspect this "
    "service stderr tail.', 'service': 'truealpha/data_engine', 'summary': 'record', "
    "'task': 'ta-data-engine.sync'}], 'success': False}, 'status': 'failed', 'triggered_by': 'deploy_v2'}\n"
)


def test_failure_alert_names_each_service_and_its_error_kind() -> None:
    """The v1.1.57 shape (2026-09-07 08:40Z): the runner's structured diagnostics reach the
    page instead of a red checkmark (#658)."""
    results = [
        {
            "service": "platform/alerting,truealpha/data_engine",
            "type": "staging",
            "returncode": 1,
            "stdout": "",
            "stderr": _RUNNER_FINAL_STDERR,
        }
    ]
    summary = reconcile.failure_summary_from_result(results[0])
    assert [entry["error_kind"] for entry in summary] == [
        "onepassword_auth_failed",
        "unknown_invoke_failure",
    ]
    text = reconcile.failure_alert_text(
        after="v1.1.57", results=results, run_url="https://x/runs/1", promote_prod=False
    )
    assert text is not None and text.startswith(
        "🚨 IaC reconcile FAILED for v1.1.57 (staging soak)"
    )
    assert (
        "- staging platform/alerting: onepassword_auth_failed — OP_SERVICE_ACCOUNT_TOKEN is invalid"
        in text
    )
    assert (
        "- staging truealpha/data_engine: unknown_invoke_failure — Inspect this service stderr tail."
        in text
    )
    assert text.endswith("Run: https://x/runs/1")


def test_failure_alert_falls_back_to_the_stderr_tail_and_is_silent_on_success() -> None:
    failed = [
        {
            "service": "platform/minio",
            "type": "prod",
            "returncode": 1,
            "stderr": (
                "deploy_v2 failed: Client error '404 Not Found' for url "
                "'https://iac.example/deploy/status'\n"
                "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/404\n"
            ),
        }
    ]
    text = reconcile.failure_alert_text(
        after="v1.1.64", results=failed, run_url="u", promote_prod=True
    )
    assert text is not None
    assert "(PRODUCTION promotion)" in text
    expected = "- prod platform/minio: deploy_v2 failed: Client error '404 Not Found'"
    assert expected in text
    assert "developer.mozilla.org" not in text
    assert reconcile.failure_summary_from_result(failed[0]) == []
    assert (
        reconcile.failure_alert_text(
            after="v1.1.64",
            results=[{"returncode": 0}],
            run_url="u",
            promote_prod=False,
        )
        is None
    )


def test_reconcile_workflow_pages_on_failure_with_the_runner_diagnostics() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["reconcile"]
    for key in (
        "INFRA2_OUT_OF_BAND_ALERT_DELIVERY_MODE",
        "INFRA2_OUT_OF_BAND_FEISHU_WEBHOOK_URL",
        "INFRA2_OUT_OF_BAND_FEISHU_APP_ID",
        "INFRA2_OUT_OF_BAND_FEISHU_APP_SECRET",
        "INFRA2_OUT_OF_BAND_FEISHU_CHAT_ID",
        "INFRA2_OUT_OF_BAND_FEISHU_API_BASE",
    ):
        assert job["env"][key] == "${{ secrets.%s }}" % key
    names = [step.get("name") for step in job["steps"]]
    alert = job["steps"][names.index("Alert on failure")]
    assert (
        names.index("Reconcile changed IaC inputs")
        < names.index("Alert on failure")
        < names.index("Upload reconcile result")
    )
    assert alert["if"].startswith("failure()") and "inputs.dry_run" in alert["if"]
    assert "deliver_out_of_band_alert" in alert["run"] and "alert_text" in alert["run"]


def test_github_run_url_is_real_or_clearly_absent() -> None:
    env = {
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "o/r",
        "GITHUB_RUN_ID": "7",
    }
    assert reconcile.github_run_url(env) == "https://github.com/o/r/actions/runs/7"
    assert (
        reconcile.github_run_url({}) == "(run URL unavailable outside GitHub Actions)"
    )
    assert "actions/runs" not in reconcile.github_run_url(
        {"GITHUB_SERVER_URL": "https://github.com"}
    )


def test_env_flag_selects_staging_or_prod_commands(monkeypatch, capsys) -> None:
    """#940: --env prod|staging selects the target environment explicitly alongside --promote-prod."""
    commands = _alerting_commands()
    assert [
        c.deploy_type
        for c in commands_to_apply(commands, dry_run=False, target_env="staging")
    ] == ["staging"]
    assert [
        c.deploy_type
        for c in commands_to_apply(commands, dry_run=False, target_env="prod")
    ] == ["prod"]

    monkeypatch.setattr(
        reconcile, "assert_production_marker_advances", lambda *_a, **_k: "v1.2.2"
    )
    rc = main(
        [
            "--after",
            "v1.2.3",
            "--before",
            "v1.2.2",
            "--env",
            "prod",
            "--dry-run",
            "--changed-file",
            "platform/12.alerting/compose.yaml",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["target_env"] == "prod"
    assert out["promote_prod"] is True


def test_release_markers_ssot_no_prod_marker_file_duplication() -> None:
    """#939 SSOT guard: production marker uses git tag production/vX.Y.Z as its single source
    of truth; neither release_markers nor reconcile_iac_inputs defines PROD_MARKER_FILE or
    duplicates RELEASE_TAG_RE / PRODUCTION_MARKER_PREFIX."""
    from common.deploy import release_markers

    assert not hasattr(release_markers, "PROD_MARKER_FILE")
    assert not hasattr(release_markers, "read_prod_marker_file")
    assert not hasattr(reconcile, "write_prod_marker_file")
    assert (
        reconcile.PRODUCTION_MARKER_PREFIX is release_markers.PRODUCTION_MARKER_PREFIX
    )
    assert reconcile.RELEASE_TAG_RE is release_markers.RELEASE_TAG_RE
    status = release_markers.marker_status_from_tags(
        ("v1.2.8", "v1.2.9"), ("production/v1.2.8",)
    )
    assert status.production_marker == "v1.2.8"
    assert status.newest_tag == "v1.2.9"
    assert status.releases_behind == 1


def test_run_hook_slot_fails_closed_without_manifest_or_service_coordinates() -> None:
    """#1416: --run-hook-slot must fail closed when neither --hooks-manifest nor
    (--hook-service + --hook-source-sha) is provided."""
    with pytest.raises(
        SystemExit,
        match="--run-hook-slot requires either --hooks-manifest or both --hook-service and --hook-source-sha",
    ):
        main(["--after", "v1.2.3", "--run-hook-slot", "smoke"])


def test_validate_evidence_json_returns_immediately_without_running_reconcile(
    monkeypatch, capsys
) -> None:
    """#1416: --validate-evidence-json validates the DeployRequest and returns 0 without
    falling through into reconcile planning or deployment."""
    monkeypatch.setattr(
        reconcile,
        "build_plan",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("build_plan must not run on --validate-evidence-json")
        ),
    )
    payload = json.dumps(
        {
            "contract_version": 1,
            "request_id": "req-1416-staging",
            "operation": "deploy",
            "service": "finance_report/app",
            "deploy_type": "staging",
            "version_ref": "v0.2.46",
            "source_repository": "wangzitian0/finance_report",
            "source_sha": SHA,
            "evidence": {
                "source_run_url": "https://github.com/wangzitian0/finance_report/actions/runs/100"
            },
        }
    )
    rc = main(["--after", "v1.2.3", "--validate-evidence-json", payload])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["max_age_seconds"] > 0


def test_reconcile_workflow_folds_in_bootstrap_and_observability_apply_on_promote_prod() -> (
    None
):
    """#1416 & #1486: reconcile-iac-inputs.yml runs the health-gated in-train IaC Runner
    bootstrap update (before tools.reconcile_iac_inputs) on non-dry-run reconciles in both
    Stage 2 staging soak and Stage 3 prod promotion, and folds in the SigNoz observability
    apply (after tools.reconcile_iac_inputs) when promote_prod=true."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["reconcile"]
    assert job["env"]["SIGNOZ_API_KEY"] == "${{ secrets.SIGNOZ_API_KEY }}"

    steps = job["steps"]
    names = [step.get("name") for step in steps]
    assert "Install uv" in names
    reconcile_idx = names.index("Reconcile changed IaC inputs")
    obs_idx = names.index("Reconcile SigNoz observability (alerts + dashboard)")
    assert reconcile_idx < obs_idx

    reconcile_step = steps[reconcile_idx]
    script = reconcile_step["run"]
    non_dry_run_idx = script.index('if [[ "$dry_run" != "true" ]]; then')
    bootstrap_idx = script.index("< scripts/deploy_iac_runner_bootstrap.sh")
    health_idx = script.index("IaC Runner public health attempt ${attempt}/12")
    run_reconcile_idx = script.index("\npython -m tools.reconcile_iac_inputs")
    assert non_dry_run_idx < bootstrap_idx < health_idx < run_reconcile_idx

    obs_step = steps[obs_idx]
    assert (
        obs_step["if"]
        == "github.event_name == 'workflow_dispatch' && inputs.promote_prod && !inputs.dry_run"
        or obs_step["if"]
        == "${{ github.event_name == 'workflow_dispatch' && inputs.promote_prod && !inputs.dry_run }}"
    )
    assert obs_step["env"]["DEPLOY_ENV"] == "production"
    obs_script = obs_step["run"]
    alerts_idx = obs_script.index("fr-observability.shared.apply-alerts")
    dashboard_idx = obs_script.index("fr-observability.shared.apply-dashboard")
    assert alerts_idx < dashboard_idx


def test_post_deploy_app_hooks_execute_smoke_and_record_results(
    monkeypatch, capsys, tmp_path
) -> None:
    """#1416 (M2): --run-post-deploy-hooks resolves each deployed App's release_tag SHA
    and environment URL, fetches runtime-hooks.manifest.json, executes the smoke slot,
    and records hook_results in the reconcile JSON payload."""
    from infra2_sdk.manifests import HOOK_SLOTS, HookRunResult, RuntimeHooksManifest
    from infra2_sdk.refs import ResolvedRef

    repos_yaml = tmp_path / "harness" / "repos.yaml"
    repos_yaml.parent.mkdir(parents=True, exist_ok=True)
    repos_yaml.write_text(
        "schema_version: 1\n"
        "repositories:\n"
        "  - id: finance-report\n"
        "    role: external-application\n"
        "    release_tag: v0.2.47\n"
        "  - id: truealpha\n"
        "    role: external-application\n"
        "    release_tag: v0.2.80\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(reconcile, "assert_after_on_main", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        reconcile,
        "run_deploy_commands",
        lambda cmds, **_kw: [
            {
                "service": c.service,
                "type": c.deploy_type,
                "returncode": 0,
                "stdout": "{}",
                "stderr": "",
            }
            for c in cmds
        ],
    )
    monkeypatch.setattr(
        reconcile,
        "resolve_image_ref",
        lambda ref, **_kw: ResolvedRef(sha=SHA, image_ref=ref, form="tag"),
    )

    manifest = RuntimeHooksManifest.from_dict(
        {
            "schema_version": 1,
            "service": "finance_report/app",
            "slots": {
                slot: {
                    "run": "echo ok",
                    "bootstrap": "",
                    "timeout_seconds": 30,
                    "required_env": [],
                    "optional_env": [],
                    "artifacts_dir": f"artifacts/{slot}",
                }
                for slot in HOOK_SLOTS
            },
        }
    )
    fetched: list[tuple[str, str]] = []
    executed: list[tuple[str, str, str, str]] = []

    def _fake_fetch(service: str, sha: str, **_kw):
        fetched.append((service, sha))
        return manifest

    def _fake_exec(
        _m, slot: str, *, app_url: str, environment: str, version_ref: str, **_kw
    ):
        executed.append((slot, app_url, environment, version_ref))
        return HookRunResult(
            service="finance_report/app",
            slot=slot,
            environment=environment,
            app_url=app_url,
            version_ref=version_ref,
            status="pass",
            exit_code=0,
            duration_seconds=0.12,
        )

    monkeypatch.setattr(reconcile, "fetch_hooks_manifest", _fake_fetch)
    monkeypatch.setattr(reconcile, "execute_app_hook_slot", _fake_exec)

    rc = main(
        [
            "--repo-root",
            str(tmp_path),
            "--after",
            "v3.1.23",
            "--changed-file",
            "harness/repos.yaml",
            "--run-post-deploy-hooks",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert fetched == [("finance_report/app", SHA), ("truealpha/app", SHA)]
    assert executed == [
        ("smoke", "https://report-staging.zitian.party", "staging", "v0.2.47"),
        ("staging_gate", "https://report-staging.zitian.party", "staging", "v0.2.47"),
        ("smoke", "https://truealpha-staging.truealpha.club", "staging", "v0.2.80"),
        (
            "staging_gate",
            "https://truealpha-staging.truealpha.club",
            "staging",
            "v0.2.80",
        ),
    ]
    assert len(out["hook_results"]) == 4


def test_post_deploy_app_hooks_failure_blocks_prod_marker_and_returns_nonzero(
    monkeypatch, capsys, tmp_path
) -> None:
    """#1416 (M2): when a post-deploy smoke hook fails during promote_prod, the production
    marker must NOT advance and main() must return exit code 1."""
    from infra2_sdk.manifests import HOOK_SLOTS, HookRunResult, RuntimeHooksManifest
    from infra2_sdk.refs import ResolvedRef

    repos_yaml = tmp_path / "harness" / "repos.yaml"
    repos_yaml.parent.mkdir(parents=True, exist_ok=True)
    repos_yaml.write_text(
        "schema_version: 1\n"
        "repositories:\n"
        "  - id: finance-report\n"
        "    role: external-application\n"
        "    release_tag: v0.2.47\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(reconcile, "assert_after_on_main", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        reconcile, "assert_production_marker_advances", lambda *_a, **_kw: "v3.1.22"
    )
    marker_recorded: list[str] = []
    monkeypatch.setattr(
        reconcile,
        "record_production_marker",
        lambda tag, *_a, **_kw: marker_recorded.append(tag) or f"production/{tag}",
    )
    monkeypatch.setattr(
        reconcile,
        "run_deploy_commands",
        lambda cmds, **_kw: [
            {
                "service": c.service,
                "type": c.deploy_type,
                "returncode": 0,
                "stdout": "{}",
                "stderr": "",
            }
            for c in cmds
        ],
    )
    monkeypatch.setattr(
        reconcile,
        "resolve_image_ref",
        lambda ref, **_kw: ResolvedRef(sha=SHA, image_ref=ref, form="tag"),
    )
    manifest = RuntimeHooksManifest.from_dict(
        {
            "schema_version": 1,
            "service": "finance_report/app",
            "slots": {
                slot: {
                    "run": "exit 1",
                    "bootstrap": "",
                    "timeout_seconds": 30,
                    "required_env": [],
                    "optional_env": [],
                    "artifacts_dir": f"artifacts/{slot}",
                }
                for slot in HOOK_SLOTS
            },
        }
    )
    monkeypatch.setattr(reconcile, "fetch_hooks_manifest", lambda *_a, **_kw: manifest)
    monkeypatch.setattr(
        reconcile,
        "execute_app_hook_slot",
        lambda *_a, **_kw: HookRunResult(
            service="finance_report/app",
            slot="smoke",
            environment="production",
            app_url="https://report.zitian.party",
            version_ref="v0.2.47",
            status="fail",
            exit_code=1,
            duration_seconds=0.05,
            summary="smoke check failed",
        ),
    )

    rc = main(
        [
            "--repo-root",
            str(tmp_path),
            "--before",
            "v3.1.22",
            "--after",
            "v3.1.23",
            "--promote-prod",
            "--changed-file",
            "harness/repos.yaml",
            "--run-post-deploy-hooks",
        ]
    )
    assert rc == 1
    assert marker_recorded == []
    out = json.loads(capsys.readouterr().out)
    assert out["production_marker"] == ""
    assert out["hook_results"][0]["status"] == "fail"


PROMOTION_ARGV = [
    "--before",
    "v3.1.22",
    "--after",
    "v3.1.23",
    "--promote-prod",
    "--changed-file",
    "platform/12.alerting/compose.yaml",
]


def _run_promotion_main(monkeypatch, capsys, record, returncode=0):
    """Drive a prod promotion of v3.1.23: every deploy returns `returncode`."""
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(reconcile, "assert_after_on_main", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        reconcile, "assert_production_marker_advances", lambda *_a, **_kw: "v3.1.22"
    )
    monkeypatch.setattr(reconcile, "record_production_marker", record)
    monkeypatch.setattr(
        reconcile,
        "run_deploy_commands",
        lambda cmds, **_kw: [
            {
                "service": c.service,
                "type": c.deploy_type,
                "returncode": returncode,
                "stdout": "{}",
                "stderr": "",
            }
            for c in cmds
        ],
    )
    rc = main(PROMOTION_ARGV)
    lines = capsys.readouterr().out.splitlines()
    warnings = [line for line in lines if line.startswith("::warning::")]
    return rc, warnings, json.loads(lines[-1])


def test_main_stays_green_and_reports_pending_marker_when_github_rejects_push(
    monkeypatch, capsys
) -> None:
    def rejected(tag, *_a, **_kw):
        raise reconcile.ProductionMarkerRejected(
            f"cannot push production marker production/{tag}: {APP_REJECTION}"
        )

    rc, warnings, out = _run_promotion_main(monkeypatch, capsys, rejected)

    assert rc == 0
    assert warnings == [
        "::warning::production marker production/v3.1.23 is pending: GitHub rejects "
        "a GitHub App push for a commit whose workflow files differ from main "
        "(#1511). Record it by hand: see docs/ssot/ops.pipeline.md, section on "
        "the production marker."
    ]
    assert out["marker_pending"] == "production/v3.1.23"
    assert out["production_marker"] == ""
    assert out["applied"] != []
    assert len(out["results"]) == len(out["applied"])
    assert {item["returncode"] for item in out["results"]} == {0}


def test_main_reports_no_pending_marker_when_marker_is_recorded(
    monkeypatch, capsys
) -> None:
    rc, warnings, out = _run_promotion_main(
        monkeypatch, capsys, lambda tag, *_a, **_kw: f"production/{tag}"
    )

    assert rc == 0
    assert warnings == []
    assert out["marker_pending"] == ""
    assert out["production_marker"] == "production/v3.1.23"


def test_main_never_reaches_the_marker_when_a_deploy_returns_nonzero(
    monkeypatch, capsys
) -> None:
    called = []

    def record(tag, *_a, **_kw):
        called.append(tag)
        return f"production/{tag}"

    rc, warnings, out = _run_promotion_main(monkeypatch, capsys, record, returncode=1)

    assert rc == 1
    assert called == []
    assert warnings == []
    assert out["marker_pending"] == ""
    assert out["production_marker"] == ""


def test_main_does_not_swallow_a_marker_push_failure_that_is_not_the_app_rule(
    monkeypatch, capsys
) -> None:
    real = reconcile.record_production_marker

    def network_down(tag, root, **_kw):
        return real(tag, root, runner=_marker_runner(1, "network down", tag))

    with pytest.raises(SystemExit) as raised:
        _run_promotion_main(monkeypatch, capsys, network_down)

    assert raised.value.code == (
        "::error::cannot push production marker production/v3.1.23: network down"
    )


def test_main_does_not_swallow_an_unrelated_error_from_the_marker_call(
    monkeypatch, capsys
) -> None:
    def broken(tag, *_a, **_kw):
        raise RuntimeError("marker writer broke")

    with pytest.raises(RuntimeError, match="^marker writer broke$"):
        _run_promotion_main(monkeypatch, capsys, broken)


def test_sync_app_releases_updates_repos_yaml_when_remote_tag_advances(
    monkeypatch, capsys, tmp_path
) -> None:
    """#1416 (T1): --sync-app-releases --apply-repos-yaml queries upstream App tags,
    detects a newer vX.Y.Z tag than harness/repos.yaml, and updates harness/repos.yaml
    in place while preserving comments."""
    repos_yaml = tmp_path / "harness" / "repos.yaml"
    repos_yaml.parent.mkdir(parents=True, exist_ok=True)
    repos_yaml.write_text(
        "schema_version: 1\n"
        "repositories:\n"
        "  - id: finance-report\n"
        "    role: external-application\n"
        "    # keep this comment\n"
        "    release_tag: v0.2.46\n"
        "  - id: truealpha\n"
        "    role: external-application\n"
        "    release_tag: v0.2.80\n",
        encoding="utf-8",
    )

    def _fake_remote(repo_url: str, **_kw):
        if "finance_report" in repo_url:
            return (
                SHA,
                {"v0.2.46": "1" * 40, "v0.2.47": SHA},
            )
        return (
            "2" * 40,
            {"v0.2.80": "2" * 40},
        )

    monkeypatch.setattr(reconcile, "_query_remote_app_refs", _fake_remote)
    monkeypatch.setattr(reconcile, "newest_release_tag", lambda **_kw: "v3.1.22")

    rc = main(
        [
            "--repo-root",
            str(tmp_path),
            "--sync-app-releases",
            "--apply-repos-yaml",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["needs_update"] is True
    assert out["updates"] == {"finance_report/app": "v0.2.47"}
    assert out["repos_yaml_updated"] is True
    assert out["next_infra_tag"] == "v3.1.23"

    updated_text = repos_yaml.read_text(encoding="utf-8")
    assert "# keep this comment" in updated_text
    assert "release_tag: v0.2.47" in updated_text
    assert "release_tag: v0.2.80" in updated_text


def test_sync_app_releases_cut_and_push_infra_tag(
    monkeypatch, capsys, tmp_path
) -> None:
    """#1416 (T1): when --cut-infra-tag and --push-infra-tag are passed, sync_app_releases
    commits the updated harness/repos.yaml, creates next_infra_tag, and pushes HEAD:main
    and refs/tags/<next_infra_tag> to origin."""
    repos_yaml = tmp_path / "harness" / "repos.yaml"
    repos_yaml.parent.mkdir(parents=True, exist_ok=True)
    repos_yaml.write_text(
        "schema_version: 1\n"
        "repositories:\n"
        "  - id: finance-report\n"
        "    role: external-application\n"
        "    release_tag: v0.2.46\n"
        "  - id: truealpha\n"
        "    role: external-application\n"
        "    release_tag: v0.2.80\n",
        encoding="utf-8",
    )

    def _fake_remote(repo_url: str, **_kw):
        if "finance_report" in repo_url:
            return (SHA, {"v0.2.46": "1" * 40, "v0.2.47": SHA})
        return ("2" * 40, {"v0.2.80": "2" * 40})

    monkeypatch.setattr(reconcile, "_query_remote_app_refs", _fake_remote)
    monkeypatch.setattr(reconcile, "newest_release_tag", lambda **_kw: "v3.1.22")

    git_calls: list[list[str]] = []

    def _fake_runner(cmd, **_kw):
        git_calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    res = reconcile.sync_app_releases(
        tmp_path,
        apply_repos_yaml=True,
        sync_gitlinks=True,
        cut_infra_tag=True,
        push_infra_tag=True,
        runner=_fake_runner,
    )
    assert res["infra_tag_cut"] is True
    assert res["infra_tag_pushed"] is True
    assert res["next_infra_tag"] == "v3.1.23"
    assert ["git", "tag", "v3.1.23"] in git_calls
    assert ["git", "push", "origin", "HEAD:main", "refs/tags/v3.1.23"] in git_calls


def test_sync_app_releases_create_pr_pushes_branch_restores_head_and_opens_or_updates_pr(
    monkeypatch, tmp_path
) -> None:
    """#1416 (Fix 1): --create-pr commits harness/repos.yaml + gitlinks, force-pushes
    origin HEAD:refs/heads/<pr_branch>, resets the local checkout back to orig_head,
    and opens a new PR (or updates an existing open PR) via gh."""
    repos_yaml = tmp_path / "harness" / "repos.yaml"
    repos_yaml.parent.mkdir(parents=True, exist_ok=True)
    repos_yaml.write_text(
        "schema_version: 1\n"
        "repositories:\n"
        "  - id: finance-report\n"
        "    role: external-application\n"
        "    release_tag: v0.2.46\n"
        "  - id: truealpha\n"
        "    role: external-application\n"
        "    release_tag: v0.2.80\n",
        encoding="utf-8",
    )

    def _fake_remote(repo_url: str, **_kw):
        if "finance_report" in repo_url:
            return (SHA, {"v0.2.46": "1" * 40, "v0.2.47": SHA})
        return ("2" * 40, {"v0.2.80": "2" * 40})

    monkeypatch.setattr(reconcile, "_query_remote_app_refs", _fake_remote)
    monkeypatch.setattr(reconcile, "newest_release_tag", lambda **_kw: "v3.1.22")

    calls: list[list[str]] = []
    open_prs_response = "[]"

    def _runner(cmd, **_kw):
        argv = [str(x) for x in cmd]
        calls.append(argv)
        if argv == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout="orig123456\n", stderr=""
            )
        if argv[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout=open_prs_response, stderr=""
            )
        if argv[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="https://github.com/wangzitian0/infra2/pull/1501\n",
                stderr="",
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    res = reconcile.sync_app_releases(
        tmp_path,
        apply_repos_yaml=True,
        sync_gitlinks=True,
        create_pr=True,
        pr_branch="chore/sync-app-releases",
        runner=_runner,
    )
    assert res["pr_created"] is True
    assert res["pr_updated"] is False
    assert res["pr_url"] == "https://github.com/wangzitian0/infra2/pull/1501"
    assert res["pr_number"] == 1501
    assert res["pr_branch"] == "chore/sync-app-releases"
    assert [
        "git",
        "push",
        "--force-with-lease",
        "origin",
        "HEAD:refs/heads/chore/sync-app-releases",
    ] in calls
    assert ["git", "reset", "--hard", "orig123456"] in calls
    assert any(c[:3] == ["gh", "pr", "create"] for c in calls)

    # Second run when an open PR already exists -> updates via gh pr edit
    repos_yaml.write_text(
        "schema_version: 1\n"
        "repositories:\n"
        "  - id: finance-report\n"
        "    role: external-application\n"
        "    release_tag: v0.2.46\n"
        "  - id: truealpha\n"
        "    role: external-application\n"
        "    release_tag: v0.2.80\n",
        encoding="utf-8",
    )
    calls.clear()
    open_prs_response = json.dumps(
        [{"number": 1501, "url": "https://github.com/wangzitian0/infra2/pull/1501"}]
    )
    res2 = reconcile.sync_app_releases(
        tmp_path,
        apply_repos_yaml=True,
        sync_gitlinks=True,
        create_pr=True,
        pr_branch="chore/sync-app-releases",
        runner=_runner,
    )
    assert res2["pr_created"] is False
    assert res2["pr_updated"] is True
    assert res2["pr_number"] == 1501
    assert any(c[:4] == ["gh", "pr", "edit", "1501"] for c in calls)


_REAL_FR_HOOKS_MANIFEST_JSON = """\
{
  "schema_version": 1,
  "service": "finance_report/app",
  "slots": {
    "smoke": {
      "run": "bash tools/smoke_test.sh \\"$INFRA2_HOOK_APP_URL\\"",
      "timeout_seconds": 180
    },
    "preview_gate": {
      "bootstrap": "uv pip install --system -r tests/e2e/requirements.txt && playwright install chromium",
      "run": "bash tools/smoke_test.sh \\"$INFRA2_HOOK_APP_URL\\" && eval \\"$(python tools/test_selection.py --stage preview_minimum_e2e --shell)\\" && pytest \\"${PREVIEW_MINIMUM_E2E_TESTS[@]}\\" -v --tb=short -o addopts=\\"\\" -m \\"$PREVIEW_MINIMUM_E2E_MARKER\\" && eval \\"$(python tools/test_selection.py --touched-files ${INFRA2_TOUCHED_FILES:-} --shell)\\" && ( [ ${#TOUCHED_E2E_TESTS[@]} -eq 0 ] || pytest \\"${TOUCHED_E2E_TESTS[@]}\\" -v --tb=short -o addopts=\\"\\" -m \\"$TOUCHED_E2E_MARKER\\" )",
      "timeout_seconds": 600,
      "optional_env": [
        "AI_PROVIDER",
        "AI_BASE_URL",
        "LARGE_MODEL",
        "SMALL_MODEL",
        "MULTIMODAL_MODEL"
      ]
    },
    "staging_gate": {
      "bootstrap": "uv pip install --system -r tests/e2e/requirements.txt && playwright install chromium",
      "run": "bash tools/smoke_test.sh \\"$INFRA2_HOOK_APP_URL\\" && eval \\"$(python tools/test_selection.py --stage staging_core_e2e --shell)\\" && pytest \\"${STAGING_CORE_E2E_TESTS[@]}\\" -v --tb=short -o addopts=\\"\\" -m \\"$STAGING_CORE_E2E_MARKER\\" && pytest tests/e2e/test_ai_provider_connectivity.py -v --tb=short -o addopts=\\"\\" && eval \\"$(python tools/test_selection.py --stage staging_ai_ocr --corpus canary --shell)\\" && pytest \\"${STAGING_AI_OCR_TESTS[@]}\\" -v --tb=short -o addopts=\\"\\" -m \\"$STAGING_AI_OCR_MARKER\\"",
      "timeout_seconds": 1800,
      "required_env": [
        "AI_API_KEY"
      ],
      "optional_env": [
        "AI_PROVIDER",
        "AI_BASE_URL",
        "LARGE_MODEL",
        "SMALL_MODEL",
        "MULTIMODAL_MODEL",
        "TEST_USER_EMAIL",
        "TEST_USER_PASSWORD"
      ]
    },
    "scheduled_audit": {
      "bootstrap": "uv pip install --system -r tests/e2e/requirements.txt && playwright install chromium",
      "run": "eval \\"$(python tools/test_selection.py --stage staging_ai_ocr --corpus audit_replay --shell)\\" && pytest \\"${STAGING_AI_OCR_TESTS[@]}\\" -v --tb=short -o addopts=\\"\\" -m \\"$STAGING_AI_OCR_MARKER\\" && eval \\"$(python tools/test_selection.py --stage staging_ai_ocr --corpus core_journey --shell)\\" && pytest \\"${STAGING_AI_OCR_TESTS[@]}\\" -v --tb=short -o addopts=\\"\\" -m \\"$STAGING_AI_OCR_MARKER\\"",
      "timeout_seconds": 2400,
      "required_env": [
        "AI_API_KEY"
      ],
      "optional_env": [
        "AI_PROVIDER",
        "AI_BASE_URL",
        "LARGE_MODEL",
        "SMALL_MODEL",
        "MULTIMODAL_MODEL"
      ]
    }
  }
}
"""

_REAL_TA_HOOKS_MANIFEST_JSON = """\
{
  "schema_version": 1,
  "service": "truealpha/app",
  "slots": {
    "smoke": {
      "run": "uv run python tools/health_check.py \\"$APP_URL/api/health\\" \\"$APP_VERSION_REF\\" 36 && uv run python tools/surface_contract.py \\"$APP_URL\\"",
      "timeout_seconds": 420,
      "required_env": [
        "APP_URL",
        "APP_VERSION_REF"
      ]
    },
    "preview_gate": {
      "bootstrap": "cd apps/app-web && bun install --frozen-lockfile && bunx playwright install --with-deps chromium",
      "run": "uv run python tools/health_check.py \\"$APP_URL/api/health\\" \\"$APP_VERSION_REF\\" 36 && bash tools/warm_surface.sh \\"$APP_URL\\" / /research/rankings && (cd apps/app-web && TA_BASE_URL=\\"$APP_URL\\" node e2e/walk-tree.mjs) && uv run python tools/surface_contract.py --authenticated \\"$APP_URL\\"",
      "timeout_seconds": 900,
      "required_env": [
        "APP_URL",
        "APP_VERSION_REF",
        "TA_EMAIL",
        "TA_MEMBER_EMAIL",
        "TA_PASSWORD",
        "TA_MCP_TOKEN"
      ]
    },
    "staging_gate": {
      "bootstrap": "cd apps/app-web && bun install --frozen-lockfile && bunx playwright install --with-deps chromium",
      "run": "uv run python tools/health_check.py \\"$APP_URL/api/health\\" \\"$APP_VERSION_REF\\" 36 && bash tools/warm_surface.sh \\"$APP_URL\\" / /research/rankings && (cd apps/app-web && TA_BASE_URL=\\"$APP_URL\\" node e2e/walk-tree.mjs) && uv run python tools/surface_contract.py --authenticated \\"$APP_URL\\"",
      "timeout_seconds": 900,
      "required_env": [
        "APP_URL",
        "APP_VERSION_REF",
        "TA_EMAIL",
        "TA_MEMBER_EMAIL",
        "TA_PASSWORD",
        "TA_MCP_TOKEN"
      ]
    },
    "scheduled_audit": {
      "bootstrap": "uv sync --frozen",
      "run": "uv run python tools/nightly_verdicts.py \\"$APP_URL/api/health\\" --environment \\"$ENVIRONMENT\\" --repo . && uv run python tools/surface_contract.py \\"$APP_URL\\"",
      "timeout_seconds": 600,
      "required_env": [
        "APP_URL",
        "ENVIRONMENT"
      ]
    }
  }
}
"""


def test_real_app_runtime_hooks_manifests_execute_unmocked_env_and_cwd(
    monkeypatch, tmp_path
) -> None:
    """#1416 (Fix 2): load the real repos/finance_report/runtime-hooks.manifest.json and
    repos/truealpha/runtime-hooks.manifest.json contracts and execute execute_app_hook_slot
    and run_post_deploy_app_hooks without mocking execute_app_hook_slot or build_hook_env."""
    from common.deploy.app_contract import execute_app_hook_slot, load_hooks_manifest
    from infra2_sdk.refs import ResolvedRef

    # Prepare an isolated repo_root in tmp_path with repos/finance_report and repos/truealpha
    # containing the exact verbatim manifests, and verify against on-disk submodules if checked out.
    fr_sub = tmp_path / "repos" / "finance_report"
    ta_sub = tmp_path / "repos" / "truealpha"
    fr_sub.mkdir(parents=True, exist_ok=True)
    ta_sub.mkdir(parents=True, exist_ok=True)
    fr_manifest_path = fr_sub / "runtime-hooks.manifest.json"
    ta_manifest_path = ta_sub / "runtime-hooks.manifest.json"
    fr_manifest_path.write_text(_REAL_FR_HOOKS_MANIFEST_JSON, encoding="utf-8")
    ta_manifest_path.write_text(_REAL_TA_HOOKS_MANIFEST_JSON, encoding="utf-8")

    fr_manifest = load_hooks_manifest(fr_manifest_path)
    ta_manifest = load_hooks_manifest(ta_manifest_path)

    disk_fr = ROOT / "repos" / "finance_report" / "runtime-hooks.manifest.json"
    disk_ta = ROOT / "repos" / "truealpha" / "runtime-hooks.manifest.json"
    if disk_fr.is_file():
        assert load_hooks_manifest(disk_fr).to_dict() == fr_manifest.to_dict()
    if disk_ta.is_file():
        assert load_hooks_manifest(disk_ta).to_dict() == ta_manifest.to_dict()

    invocations: list[tuple[list[str], Path | None, dict[str, str]]] = []

    def _capturing_runner(cmd, *, env=None, cwd=None, **_kw):
        invocations.append((list(cmd), Path(cwd) if cwd else None, dict(env or {})))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    # 1. truealpha smoke (declares required_env: ["APP_URL", "APP_VERSION_REF"])
    res_ta_smoke = execute_app_hook_slot(
        ta_manifest,
        "smoke",
        app_url="https://truealpha-staging.truealpha.club",
        environment="staging",
        version_ref="v0.2.80",
        cwd=ta_sub,
        ambient_env={"PATH": "/usr/bin:/bin"},
        runner=_capturing_runner,
    )
    assert res_ta_smoke.status == "pass"
    _, ta_cwd, ta_env = invocations[-1]
    assert ta_cwd == ta_sub
    assert ta_env["APP_URL"] == "https://truealpha-staging.truealpha.club"
    assert ta_env["APP_VERSION_REF"] == "v0.2.80"

    # 2. truealpha scheduled_audit (declares required_env: ["APP_URL", "ENVIRONMENT"])
    res_ta_audit = execute_app_hook_slot(
        ta_manifest,
        "scheduled_audit",
        app_url="https://truealpha-staging.truealpha.club",
        environment="staging",
        version_ref="v0.2.80",
        cwd=ta_sub,
        ambient_env={"PATH": "/usr/bin:/bin"},
        runner=_capturing_runner,
    )
    assert res_ta_audit.status == "pass"
    _, _, ta_audit_env = invocations[-1]
    assert ta_audit_env["ENVIRONMENT"] == "staging"

    # 3. finance_report smoke (uses BASE_URL="$INFRA2_HOOK_APP_URL" bash tools/smoke_test.sh)
    res_fr_smoke = execute_app_hook_slot(
        fr_manifest,
        "smoke",
        app_url="https://report-staging.zitian.party",
        environment="staging",
        version_ref="v0.2.47",
        cwd=fr_sub,
        ambient_env={"PATH": "/usr/bin:/bin"},
        runner=_capturing_runner,
    )
    assert res_fr_smoke.status == "pass"
    _, fr_cwd, fr_env = invocations[-1]
    assert fr_cwd == fr_sub
    assert fr_env["INFRA2_HOOK_APP_URL"] == "https://report-staging.zitian.party"
    assert fr_env["APP_URL"] == "https://report-staging.zitian.party"
    assert fr_env["BASE_URL"] == "https://report-staging.zitian.party"

    # 4. run_post_deploy_app_hooks uses repos/<app> cwd when present, or fetches temp checkout
    (tmp_path / "harness").mkdir(parents=True, exist_ok=True)
    (tmp_path / "harness" / "repos.yaml").write_text(
        (ROOT / "harness" / "repos.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        reconcile,
        "resolve_image_ref",
        lambda ref, **_kw: ResolvedRef(sha=SHA, image_ref=ref, form="tag"),
    )
    plan = build_plan(
        ["harness/repos.yaml"],
        repo_root=tmp_path,
        app_release_tags={
            "finance_report/app": "v0.2.47",
            "truealpha/app": "v0.2.80",
        },
    )
    applied = [
        reconcile.DeployCommand(
            service="finance_report/app", deploy_type="staging", argv=["true"]
        ),
        reconcile.DeployCommand(
            service="truealpha/app", deploy_type="staging", argv=["true"]
        ),
    ]
    invocations.clear()
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    hook_results = reconcile.run_post_deploy_app_hooks(
        plan,
        applied,
        repo_root=tmp_path,
        after="v3.1.24",
        target_env="staging",
        manifest_fetcher=lambda url: (
            fr_manifest_path.read_bytes()
            if "finance_report" in url
            else ta_manifest_path.read_bytes()
        ),
        runner=_capturing_runner,
        record_issues=False,
    )
    assert [r["status"] for r in hook_results] == ["pass", "pass"]
    assert hook_results[0]["cwd"] == str(fr_sub)
    assert hook_results[1]["cwd"] == str(ta_sub)


def test_record_app_hook_issue_trail_opens_and_closes_app_issues() -> None:
    """#1416 (Fix 3): record_app_hook_issue_trail opens an issue on the target App repo
    when a post-deploy hook fails and closes it when the hook passes on a subsequent run."""

    class _FakeIssueApi:
        def __init__(self, repo: str) -> None:
            self.repo = repo
            self.issues: list[dict] = []
            self.comments: list[tuple[int, str]] = []
            self.closed: list[int] = []

        def open_issues(self, *, with_body: bool = False) -> list[dict]:
            return list(self.issues)

        def create(self, title: str, body: str, labels) -> int:
            num = len(self.issues) + 10
            self.issues.append({"number": num, "title": title, "body": body})
            return num

        def comment(self, number: int, body: str) -> None:
            self.comments.append((number, body))

        def close(self, number: int) -> None:
            self.closed.append(number)

    apis: dict[str, _FakeIssueApi] = {}

    def _factory(repo: str, _token: str) -> _FakeIssueApi:
        return apis.setdefault(repo, _FakeIssueApi(repo))

    env = {
        "GITHUB_TOKEN": "tok-123",
        "GITHUB_EVENT_NAME": "schedule",
        "GITHUB_REPOSITORY": "wangzitian0/infra2",
    }
    rc_fail = reconcile.record_app_hook_issue_trail(
        [
            {
                "service": "truealpha/app",
                "slot": "smoke",
                "status": "fail",
                "app_url": "https://truealpha-staging.truealpha.club",
                "version_ref": "v0.2.80",
                "summary": "run exited with code 1",
                "stderr_tail": "HTTP 502 Bad Gateway",
            }
        ],
        target_env="staging",
        run_url="https://github.com/wangzitian0/infra2/actions/runs/999",
        env=env,
        issues_factory=_factory,
    )
    assert rc_fail == 0
    assert "wangzitian0/truealpha" in apis
    ta_api = apis["wangzitian0/truealpha"]
    assert len(ta_api.issues) == 1
    assert "app-hook:truealpha/app:smoke:staging" in ta_api.issues[0]["title"]

    # Subsequent passing run closes the issue
    rc_pass = reconcile.record_app_hook_issue_trail(
        [
            {
                "service": "truealpha/app",
                "slot": "smoke",
                "status": "pass",
                "app_url": "https://truealpha-staging.truealpha.club",
                "version_ref": "v0.2.80",
                "summary": "smoke passed",
                "stderr_tail": "",
            }
        ],
        target_env="staging",
        run_url="https://github.com/wangzitian0/infra2/actions/runs/1000",
        env=env,
        issues_factory=_factory,
    )
    assert rc_pass == 0
    assert ta_api.closed == [10]


def test_audit_app_hooks_cli_validates_both_apps(monkeypatch, capsys, tmp_path) -> None:
    """#1416 (Fix 3): --audit-app-hooks validates both finance_report/app and truealpha/app
    runtime-hooks.manifest.json files (both from local checkouts and via remote fetch when
    submodules are absent in CI) and exits 0."""
    from infra2_sdk.refs import ResolvedRef

    (tmp_path / "harness").mkdir(parents=True, exist_ok=True)
    (tmp_path / "harness" / "repos.yaml").write_text(
        (ROOT / "harness" / "repos.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        reconcile,
        "resolve_image_ref",
        lambda ref, **_kw: ResolvedRef(sha=SHA, image_ref=ref, form="tag"),
    )
    monkeypatch.setattr(
        reconcile,
        "fetch_hooks_manifest",
        lambda svc, sha, **_kw: reconcile.RuntimeHooksManifest.from_dict(
            json.loads(
                _REAL_FR_HOOKS_MANIFEST_JSON
                if svc == "finance_report/app"
                else _REAL_TA_HOOKS_MANIFEST_JSON
            )
        ),
    )
    rc = main(["--repo-root", str(tmp_path), "--audit-app-hooks"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["services"]["finance_report/app"]["ok"] is True
    assert out["services"]["truealpha/app"]["ok"] is True


def test_sync_app_releases_create_pr_surfaces_gh_stderr_on_failure(
    monkeypatch, capsys, tmp_path
) -> None:
    """#1463: when `gh pr create` or `gh pr edit` fails with CalledProcessError,
    sync_app_releases writes the captured stderr to sys.stderr before re-raising."""
    repos_yaml = tmp_path / "harness" / "repos.yaml"
    repos_yaml.parent.mkdir(parents=True, exist_ok=True)
    repos_yaml.write_text(
        "schema_version: 1\n"
        "repositories:\n"
        "  - id: finance-report\n"
        "    role: external-application\n"
        "    release_tag: v0.2.46\n"
        "  - id: truealpha\n"
        "    role: external-application\n"
        "    release_tag: v0.2.80\n",
        encoding="utf-8",
    )

    def _fake_remote(repo_url: str, **_kw):
        if "finance_report" in repo_url:
            return (SHA, {"v0.2.46": "1" * 40, "v0.2.47": SHA})
        return ("2" * 40, {"v0.2.80": "2" * 40})

    monkeypatch.setattr(reconcile, "_query_remote_app_refs", _fake_remote)
    monkeypatch.setattr(reconcile, "newest_release_tag", lambda **_kw: "v3.1.22")

    open_prs_response = "[]"

    def _failing_runner(cmd, **_kw):
        argv = [str(x) for x in cmd]
        if argv == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout="orig123456\n", stderr=""
            )
        if argv[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout=open_prs_response, stderr=""
            )
        if argv[:3] in (["gh", "pr", "create"], ["gh", "pr", "edit"]):
            raise subprocess.CalledProcessError(
                1,
                argv,
                output="",
                stderr="GraphQL: GitHub Actions is not permitted to create or approve pull requests",
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with pytest.raises(subprocess.CalledProcessError):
        reconcile.sync_app_releases(
            tmp_path,
            apply_repos_yaml=True,
            sync_gitlinks=True,
            create_pr=True,
            pr_branch="chore/sync-app-releases",
            runner=_failing_runner,
        )
    err = capsys.readouterr().err
    assert (
        "gh pr command failed (exit 1): GraphQL: GitHub Actions is not permitted to create or approve pull requests"
        in err
    )


def test_reconcile_workflow_skips_deploy_outage_alert_when_sync_app_releases_fails(
    monkeypatch, tmp_path, capsys
) -> None:
    """#1463: reconcile-iac-inputs.yml records RECONCILE_PHASE=sync-app-releases before
    the pull-based App release PR sync and RECONCILE_PHASE=reconcile-deploy before the tag
    deploy step, and Alert on failure skips deliver_out_of_band_alert when a failure occurs
    during sync-app-releases before reconcile-result.json exists."""
    import tools.out_of_band_watchdog as watchdog

    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["reconcile"]["steps"]
    names = [step.get("name") for step in steps]
    reconcile_step = steps[names.index("Reconcile changed IaC inputs")]
    alert_step = steps[names.index("Alert on failure")]

    reconcile_script = reconcile_step["run"]
    sync_phase_idx = reconcile_script.index(
        'echo "RECONCILE_PHASE=sync-app-releases" >> "$GITHUB_ENV"'
    )
    sync_cmd_idx = reconcile_script.index(
        'python -m tools.reconcile_iac_inputs "${sync_args[@]}"'
    )
    deploy_phase_idx = reconcile_script.index(
        'echo "RECONCILE_PHASE=reconcile-deploy" >> "$GITHUB_ENV"'
    )
    deploy_cmd_idx = reconcile_script.index(
        'python -m tools.reconcile_iac_inputs "${args[@]}"'
    )
    assert sync_phase_idx < sync_cmd_idx < deploy_phase_idx < deploy_cmd_idx

    assert alert_step["env"]["RECONCILE_PHASE"] == "${{ env.RECONCILE_PHASE || '' }}"
    alert_run = alert_step["run"]
    py_body = alert_run.split("python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]

    delivered: list[str] = []
    monkeypatch.setattr(
        watchdog,
        "deliver_out_of_band_alert",
        lambda _env, text: delivered.append(text),
    )
    monkeypatch.chdir(tmp_path)

    # Case 1: failure during sync-app-releases (no reconcile-result.json) -> skip Feishu alert
    monkeypatch.setenv("RECONCILE_PHASE", "sync-app-releases")
    monkeypatch.setenv("INPUT_AFTER", "v3.1.25")
    monkeypatch.setenv("INPUT_PROMOTE_PROD", "false")
    with pytest.raises(SystemExit) as exc_info:
        exec(compile(py_body, "<alert-on-failure>", "exec"), {})
    assert exc_info.value.code == 0
    assert delivered == []
    assert (
        "App release PR sync failed before reconcile-deploy phase; skipping deploy-outage Feishu alert."
        in capsys.readouterr().out
    )

    # Case 2: failure during reconcile-deploy (no reconcile-result.json) -> pages Feishu
    monkeypatch.setenv("RECONCILE_PHASE", "reconcile-deploy")
    exec(compile(py_body, "<alert-on-failure>", "exec"), {})
    assert len(delivered) == 1
    assert "🚨 IaC reconcile FAILED for v3.1.25 (staging soak)" in delivered[0]


def test_parse_hook_slots_deduplicates_and_validates() -> None:
    """#1493: _parse_hook_slots splits comma-separated slots, strips whitespace, preserves order,
    and rejects empty or unknown slot names."""
    assert reconcile._parse_hook_slots("smoke, staging_gate , smoke") == (
        "smoke",
        "staging_gate",
    )
    with pytest.raises(ValueError, match="at least one slot"):
        reconcile._parse_hook_slots(" , ")
    with pytest.raises(ValueError, match="unknown hook slot 'nonexistent'"):
        reconcile._parse_hook_slots("smoke,nonexistent")


def test_run_post_deploy_app_hooks_executes_smoke_and_staging_gate_in_order(
    monkeypatch, tmp_path
) -> None:
    """#1493: run_post_deploy_app_hooks with slot='smoke,staging_gate' executes both smoke and
    staging_gate in order for each deployed App and records issue trails per slot."""
    from infra2_sdk.manifests import HOOK_SLOTS, HookRunResult, RuntimeHooksManifest
    from infra2_sdk.refs import ResolvedRef

    monkeypatch.setattr(
        reconcile,
        "resolve_image_ref",
        lambda ref, **_kw: ResolvedRef(sha=SHA, image_ref=ref, form="tag"),
    )
    manifest = RuntimeHooksManifest.from_dict(
        {
            "schema_version": 1,
            "service": "finance_report/app",
            "slots": {
                slot_name: {
                    "run": "echo ok",
                    "bootstrap": "",
                    "timeout_seconds": 30,
                    "required_env": [],
                    "optional_env": [],
                    "artifacts_dir": f"artifacts/{slot_name}",
                }
                for slot_name in HOOK_SLOTS
            },
        }
    )
    executed_slots: list[str] = []
    recorded_trails: list[tuple[str, list[str]]] = []

    def _fake_exec(
        _m, slot: str, *, app_url: str, environment: str, version_ref: str, **_kw
    ):
        executed_slots.append(slot)
        return HookRunResult(
            service="finance_report/app",
            slot=slot,
            environment=environment,
            app_url=app_url,
            version_ref=version_ref,
            status="pass",
            exit_code=0,
            duration_seconds=0.1,
        )

    def _fake_record(results, *, slot: str = "", **_kw):
        recorded_trails.append((slot, [str(r.get("slot")) for r in results]))
        return 0

    monkeypatch.setattr(reconcile, "execute_app_hook_slot", _fake_exec)
    monkeypatch.setattr(reconcile, "record_app_hook_issue_trail", _fake_record)
    monkeypatch.setenv("GITHUB_TOKEN", "tok-test")

    plan = build_plan(
        ["finance_report/finance_report/10.app/compose.yaml"],
        app_release_tags={"finance_report/app": "v0.2.50"},
    )
    applied = [
        reconcile.DeployCommand(
            service="finance_report/app", deploy_type="staging", argv=["true"]
        )
    ]
    results = reconcile.run_post_deploy_app_hooks(
        plan,
        applied,
        repo_root=tmp_path,
        after="v3.1.29",
        target_env="staging",
        slot="smoke,staging_gate",
        manifest_fetcher=lambda _url: json.dumps(manifest.to_dict()).encode("utf-8"),
    )
    assert executed_slots == ["smoke", "staging_gate"]
    assert [r["slot"] for r in results] == ["smoke", "staging_gate"]
    assert all(r["ok"] is True for r in results)
    assert recorded_trails == [
        ("smoke", ["smoke"]),
        ("staging_gate", ["staging_gate"]),
    ]


def test_run_scheduled_app_audits_executes_scheduled_audit_and_records_trail(
    monkeypatch, capsys, tmp_path
) -> None:
    """#1493: run_scheduled_app_audits discovers Apps declaring scheduled_audit, forwards
    AI_API_KEY via env_overrides, records the issue trail, and main(--run-scheduled-audit)
    returns 1 when any audit fails."""
    from infra2_sdk.manifests import HOOK_SLOTS, HookRunResult, RuntimeHooksManifest
    from infra2_sdk.refs import ResolvedRef

    repos_yaml = tmp_path / "harness" / "repos.yaml"
    repos_yaml.parent.mkdir(parents=True, exist_ok=True)
    repos_yaml.write_text(
        "schema_version: 1\n"
        "repositories:\n"
        "  - id: finance-report\n"
        "    role: external-application\n"
        "    release_tag: v0.2.50\n"
        "  - id: truealpha\n"
        "    role: external-application\n"
        "    release_tag: v0.2.83\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        reconcile,
        "resolve_image_ref",
        lambda ref, **_kw: ResolvedRef(sha=SHA, image_ref=ref, form="tag"),
    )
    manifest = RuntimeHooksManifest.from_dict(
        {
            "schema_version": 1,
            "service": "finance_report/app",
            "slots": {
                slot_name: {
                    "run": "echo ok",
                    "bootstrap": "",
                    "timeout_seconds": 30,
                    "required_env": ["AI_API_KEY"]
                    if slot_name == "scheduled_audit"
                    else [],
                    "optional_env": [],
                    "artifacts_dir": f"artifacts/{slot_name}",
                }
                for slot_name in HOOK_SLOTS
            },
        }
    )
    monkeypatch.setattr(reconcile, "fetch_hooks_manifest", lambda *_a, **_kw: manifest)

    seen_calls: list[tuple[str, str, str, str]] = []
    recorded_trails: list[tuple[str, int]] = []

    def _fake_exec(
        _m,
        slot: str,
        *,
        app_url: str,
        environment: str,
        version_ref: str,
        ambient_env=None,
        **_kw,
    ):
        seen_calls.append(
            (slot, app_url, version_ref, str((ambient_env or {}).get("AI_API_KEY", "")))
        )
        return HookRunResult(
            service="finance_report/app",
            slot=slot,
            environment=environment,
            app_url=app_url,
            version_ref=version_ref,
            status="fail" if "report-staging" in app_url else "pass",
            exit_code=1 if "report-staging" in app_url else 0,
            duration_seconds=0.25,
            summary="U7/U13 core journey failed"
            if "report-staging" in app_url
            else "ok",
        )

    monkeypatch.setattr(reconcile, "execute_app_hook_slot", _fake_exec)
    monkeypatch.setattr(
        reconcile,
        "record_app_hook_issue_trail",
        lambda results, *, slot="", **_kw: (
            recorded_trails.append((slot, len(results))) or 0
        ),
    )
    monkeypatch.setenv("GITHUB_TOKEN", "tok-audit")
    monkeypatch.setenv("ZAI_CODING_CN_API_KEY", "zai-secret-key")
    monkeypatch.delenv("AI_API_KEY", raising=False)

    rc = main(["--repo-root", str(tmp_path), "--run-scheduled-audit"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    results = out["scheduled_audit_results"]
    assert len(results) == 2
    assert results[0]["service"] == "finance_report/app"
    assert results[0]["ok"] is False
    assert results[1]["service"] == "truealpha/app"
    assert results[1]["ok"] is True
    assert seen_calls == [
        (
            "scheduled_audit",
            "https://report-staging.zitian.party",
            "v0.2.50",
            "zai-secret-key",
        ),
        (
            "scheduled_audit",
            "https://truealpha-staging.truealpha.club",
            "v0.2.83",
            "zai-secret-key",
        ),
    ]
    assert recorded_trails == [("scheduled_audit", 2)]


def _unwired_hook_slots(
    slots: tuple[str, ...], workflow_texts: dict[str, str]
) -> list[str]:
    """Return hook slots from ``slots`` that no workflow in ``workflow_texts`` invokes."""
    combined = "\n".join(workflow_texts.values())
    unwired: list[str] = []
    for slot in slots:
        slot_wired = False
        if slot == "scheduled_audit" and "--run-scheduled-audit" in combined:
            slot_wired = True
        if re.search(rf"\b{re.escape(slot)}\b", combined):
            slot_wired = True
        if not slot_wired:
            unwired.append(slot)
    return unwired


def test_every_declared_hook_slot_is_wired_into_workflows() -> None:
    """#1493 (AC 4): every slot in HOOK_SLOTS ('smoke', 'preview_gate', 'staging_gate',
    'scheduled_audit') is referenced and wired into .github/workflows/*.yml, and adding a
    hypothetical dead slot to HOOK_SLOTS fails the check."""
    from infra2_sdk.manifests import HOOK_SLOTS

    workflows_dir = ROOT / ".github" / "workflows"
    workflow_texts = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(workflows_dir.glob("*.y*ml"))
    }
    assert set(HOOK_SLOTS) == {
        "smoke",
        "preview_gate",
        "staging_gate",
        "scheduled_audit",
    }
    assert _unwired_hook_slots(tuple(HOOK_SLOTS), workflow_texts) == []

    # Mutation proof: a hypothetical dead slot not wired in any workflow fails closed.
    assert _unwired_hook_slots((*HOOK_SLOTS, "unwired_slot"), workflow_texts) == [
        "unwired_slot"
    ]

    # Also verify each realApp runtime-hooks.manifest.json slot is covered by HOOK_SLOTS and workflows.
    for raw_manifest in (_REAL_FR_HOOKS_MANIFEST_JSON, _REAL_TA_HOOKS_MANIFEST_JSON):
        manifest = reconcile.RuntimeHooksManifest.from_dict(json.loads(raw_manifest))
        assert _unwired_hook_slots(tuple(manifest.slots.keys()), workflow_texts) == []
