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


@pytest.fixture(autouse=True)
def _stub_app_version_ref_on_main(monkeypatch) -> None:
    """Stub the #1500 GitHub compare API guard in offline reconcile unit tests;
    dedicated tests below verify the guard is invoked and fails closed on off-main tags."""
    monkeypatch.setattr(
        reconcile, "assert_app_version_ref_on_main", lambda *_a, **_kw: None
    )


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
    "workflow `.github/workflows/reconcile-iac-inputs.yml` without `workflows` permission"
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


@pytest.mark.parametrize("bad_after", ["main", "d" * 40])
def test_main_refuses_a_staging_apply_on_a_branch_or_sha_before_provenance(
    monkeypatch, bad_after
) -> None:
    """#1628: a staging dispatch on `main` must stop before any deploy unit runs."""
    reached: list[str] = []
    monkeypatch.setattr(
        reconcile,
        "assert_after_on_main",
        lambda *_a, **_kw: reached.append("provenance"),
    )
    monkeypatch.setattr(
        reconcile, "build_plan", lambda *_a, **_kw: reached.append("plan")
    )

    with pytest.raises(SystemExit, match="requires a release-tag iac_ref"):
        main(
            [
                "--after",
                bad_after,
                "--env",
                "staging",
                "--changed-file",
                "platform/12.alerting/compose.yaml",
            ]
        )

    assert reached == []


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


def test_reconcile_job_runs_on_ubuntu_24_04_for_playwright() -> None:
    """#1543: ubuntu-latest is 26.04, where Playwright 1.57.0 has no chromium build."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    runs_on = workflow["jobs"]["reconcile"]["runs-on"]
    assert runs_on == "ubuntu-24.04"


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
    bootstrap update (before tools.reconcile_iac_inputs) in the Stage 3 prod promotion only
    (the shared runner is production; owner decision 2026-11-11), and folds in the SigNoz
    observability apply (after tools.reconcile_iac_inputs) when promote_prod=true. The
    behaviour of the update block is tested by running it below."""
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


def _runner_bootstrap_block() -> str:
    """The bash of the in-train runner update, cut out of the real workflow step."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["reconcile"]["steps"]
    script = next(
        step["run"]
        for step in steps
        if step.get("name") == "Reconcile changed IaC inputs"
    )
    start = script.index("# IaC Runner Bootstrap Update")
    end = script.index("\nargs=(--after")
    return script[start:end]


def _run_runner_block(
    tmp_path: Path,
    *,
    promote_prod: bool,
    changed_files: str,
    with_ssh_credentials: bool = True,
    blank: tuple[str, ...] = (),
    health_status: str = "200",
    ssh_exit: int = 0,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the update block with stubbed ssh, curl and sleep; return the run and the ssh calls."""
    import os

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh_log = tmp_path / "ssh.log"
    (bin_dir / "ssh").write_text(
        f'#!/bin/sh\necho "$*" >> "{ssh_log}"\ncat > /dev/null\nexit {ssh_exit}\n',
        encoding="utf-8",
    )
    (bin_dir / "curl").write_text(
        "#!/bin/sh\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "-o" ]; then echo ok > "$2"; fi\n'
        "  shift\n"
        "done\n"
        f"printf {health_status}\n",
        encoding="utf-8",
    )
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for name in ("ssh", "curl", "sleep"):
        (bin_dir / name).chmod(0o755)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "f.txt").write_text("x")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "one")
    _git(repo, "tag", "v9.9.9")
    # The block feeds this file to ssh on stdin (the real one is read by the stub).
    (repo / "scripts").mkdir()
    (repo / "scripts" / "deploy_iac_runner_bootstrap.sh").write_text("true\n")
    home = tmp_path / "home"
    home.mkdir()
    header = (
        'dry_run="false"\n'
        f'promote_prod="{"true" if promote_prod else "false"}"\n'
        'after="v9.9.9"\n'
        f"changed_files={changed_files!r}\n"
        'IAC_RUNNER_URL="https://runner.example"\n'
    )
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(home),
        "INFRA2_WATCHDOG_SSH_PRIVATE_KEY": "key" if with_ssh_credentials else "",
        "INFRA2_WATCHDOG_SSH_HOST": "host.example" if with_ssh_credentials else "",
        "INFRA2_WATCHDOG_SSH_USER": "root" if with_ssh_credentials else "",
    }
    for name in blank:
        env[name] = ""
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", header + _runner_bootstrap_block()],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    calls = ssh_log.read_text().splitlines() if ssh_log.exists() else []
    return result, calls


_RUNNER_CHANGE = "bootstrap/06.iac_runner/Dockerfile\ndocs/README.md"


def test_runner_bootstrap_update_waits_for_the_production_run(tmp_path) -> None:
    """#1486, owner decision 2026-11-11: the shared iac-runner is production. A staging soak
    whose release changes the runner files must not rebuild it; it says why and goes on."""
    result, ssh_calls = _run_runner_block(
        tmp_path, promote_prod=False, changed_files=_RUNNER_CHANGE
    )
    assert result.returncode == 0, result.stderr
    assert ssh_calls == []
    assert "::notice::The IaC Runner bootstrap changed in this release" in result.stdout
    assert "Deploy IaC Runner bootstrap changes" not in result.stdout


def test_runner_bootstrap_update_runs_in_the_production_run(tmp_path) -> None:
    result, ssh_calls = _run_runner_block(
        tmp_path, promote_prod=True, changed_files=_RUNNER_CHANGE
    )
    assert result.returncode == 0, result.stderr
    assert len(ssh_calls) == 1
    assert "root@host.example" in ssh_calls[0]
    assert "INFRA2_DEPLOY_SHA=" in ssh_calls[0]
    assert (
        "IaC Runner public health attempt 1/12: url=https://runner.example/health status=200"
        in result.stdout
    )
    assert "::notice::" not in result.stdout


@pytest.mark.parametrize("promote_prod", [False, True])
def test_runner_bootstrap_update_is_silent_when_no_runner_file_changed(
    tmp_path, promote_prod
) -> None:
    result, ssh_calls = _run_runner_block(
        tmp_path, promote_prod=promote_prod, changed_files="docs/README.md\ncommon/x.py"
    )
    assert result.returncode == 0, result.stderr
    assert ssh_calls == []
    assert "::notice::" not in result.stdout


def test_production_runner_update_refuses_to_run_without_ssh_credentials(
    tmp_path,
) -> None:
    """A production run with runner changes and no credentials fails closed: it does not
    skip the update and go on to deploy services on an old runner."""
    result, ssh_calls = _run_runner_block(
        tmp_path,
        promote_prod=True,
        changed_files=_RUNNER_CHANGE,
        with_ssh_credentials=False,
    )
    assert result.returncode == 1
    assert "INFRA2_WATCHDOG_SSH_PRIVATE_KEY is required" in result.stdout
    assert ssh_calls == []


@pytest.mark.parametrize(
    "name",
    [
        "INFRA2_WATCHDOG_SSH_PRIVATE_KEY",
        "INFRA2_WATCHDOG_SSH_HOST",
        "INFRA2_WATCHDOG_SSH_USER",
    ],
)
def test_production_runner_update_needs_each_ssh_credential(tmp_path, name) -> None:
    """Each credential alone stops the production update: a missing host or user must not
    become an `ssh root@` call."""
    result, ssh_calls = _run_runner_block(
        tmp_path, promote_prod=True, changed_files=_RUNNER_CHANGE, blank=(name,)
    )
    assert result.returncode == 1
    assert f"{name} is required" in result.stdout
    assert ssh_calls == []


def test_production_runner_update_fails_when_the_runner_stays_unhealthy(
    tmp_path,
) -> None:
    """The health gate of the production run: a runner that answers 503 after the update stops
    the release before any service deploys on it."""
    result, ssh_calls = _run_runner_block(
        tmp_path,
        promote_prod=True,
        changed_files=_RUNNER_CHANGE,
        health_status="503",
    )
    assert result.returncode == 1
    assert len(ssh_calls) == 1
    assert "IaC Runner public health attempt 12/12" in result.stdout
    assert (
        "IaC Runner public health stayed unhealthy after 12 attempts" in result.stdout
    )


def test_production_runner_update_fails_when_the_ssh_deploy_fails(tmp_path) -> None:
    result, ssh_calls = _run_runner_block(
        tmp_path, promote_prod=True, changed_files=_RUNNER_CHANGE, ssh_exit=1
    )
    assert result.returncode != 0
    assert len(ssh_calls) == 1
    assert "IaC Runner public health attempt" not in result.stdout


def test_post_deploy_app_hooks_execute_smoke_and_record_results(
    monkeypatch, capsys, tmp_path
) -> None:
    """#1416 (M2): post-deploy hooks resolve each deployed App's release_tag SHA
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
            "schema_version": 2,
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
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert sorted(fetched) == [("finance_report/app", SHA), ("truealpha/app", SHA)]
    assert sorted(executed) == sorted(
        [
            ("smoke", "https://report-staging.zitian.party", "staging", "v0.2.47"),
            (
                "staging_gate",
                "https://report-staging.zitian.party",
                "staging",
                "v0.2.47",
            ),
            ("smoke", "https://truealpha-staging.truealpha.club", "staging", "v0.2.80"),
            (
                "staging_gate",
                "https://truealpha-staging.truealpha.club",
                "staging",
                "v0.2.80",
            ),
        ]
    )
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
            "schema_version": 2,
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


def _run_promotion_main(monkeypatch, capsys, record, returncode=0, extra_args=()):
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
    rc = main([*PROMOTION_ARGV, *extra_args])
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


def test_main_does_not_reach_the_marker_call_in_a_dry_run(monkeypatch, capsys) -> None:
    called = []

    def record(tag, *_a, **_kw):
        called.append(tag)
        raise reconcile.ProductionMarkerRejected("a dry run must not record a marker")

    rc, warnings, out = _run_promotion_main(
        monkeypatch, capsys, record, extra_args=["--dry-run"]
    )

    assert rc == 0
    assert called == []
    assert warnings == []
    assert out["dry_run"] is True
    assert out["marker_pending"] == ""
    assert out["production_marker"] == ""


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


def test_deleted_legacy_alert_and_tag_wheels_are_absent() -> None:
    """#1590: legacy tag-cutting, hardcoded lineage slot sets, issue-API proxies,
    and schedule alert-suppression duplicates must stay removed."""
    for removed in (
        "LINEAGE_HOOK_SLOTS",
        "release_ref_for_slot",
        "_RecordingIssueApi",
        "scheduled_audit_alert_text",
        "resolve_audit_job_failure_alert",
        "should_suppress_schedule_hook_alert",
    ):
        assert not hasattr(reconcile, removed), f"{removed} must not exist"
    with pytest.raises(SystemExit):
        main(["--sync-app-releases", "--cut-infra-tag"])


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
  "schema_version": 2,
  "service": "finance_report/app",
  "slots": {
    "smoke": {
      "run": "bash tools/smoke_test.sh \\"$INFRA2_HOOK_APP_URL\\"",
      "timeout_seconds": 120
    },
    "preview_gate": {
      "bootstrap": "uv pip install --system -r tests/e2e/requirements.txt && playwright install chromium",
      "run": "bash tools/smoke_test.sh \\"$INFRA2_HOOK_APP_URL\\" && python tools/test_selection.py --stage preview_minimum_e2e --run && python tools/test_selection.py --touched-files ${INFRA2_TOUCHED_FILES:-} --run",
      "timeout_seconds": 360,
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
      "run": "bash tools/smoke_test.sh \\"$INFRA2_HOOK_APP_URL\\" && python tools/test_selection.py --stage staging_core_e2e --run && STRICT_E2E_GATES=1 python -m pytest tests/e2e/test_ai_provider_connectivity.py -v --tb=short -o addopts=\\"\\" && python tools/test_selection.py --stage staging_ai_ocr --corpus canary --run",
      "timeout_seconds": 540,
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
    "prod_gate": {
      "run": "bash tools/smoke_test.sh \\"$INFRA2_HOOK_APP_URL\\"",
      "timeout_seconds": 180
    },
    "scheduled_audit": {
      "bootstrap": "uv pip install --system -r tests/e2e/requirements.txt && playwright install chromium",
      "run": "python tools/test_selection.py --stage staging_ai_ocr --corpus audit_replay --run && python tools/test_selection.py --stage staging_ai_ocr --corpus core_journey --run && python -m common.testing.benchmarks.pdf_corpus_oracle --app-url \\"$INFRA2_HOOK_APP_URL\\" --max-workers 1 && python -m common.testing.benchmarks.run_financial_scenario_benchmark --app-url \\"$INFRA2_HOOK_APP_URL\\" --case 1,2,3,4,5,6",
      "timeout_seconds": 3600,
      "optional_env": [
        "AI_PROVIDER",
        "AI_BASE_URL",
        "LARGE_MODEL",
        "SMALL_MODEL",
        "MULTIMODAL_MODEL",
        "TEST_USER_EMAIL",
        "TEST_USER_PASSWORD"
      ]
    }
  }
}
"""

_REAL_TA_HOOKS_MANIFEST_JSON = """\
{
  "schema_version": 2,
  "service": "truealpha/app",
  "slots": {
    "smoke": {
      "run": "uv run python tools/health_check.py \\"$APP_URL/api/health\\" \\"$APP_VERSION_REF\\" 36",
      "timeout_seconds": 120,
      "required_env": [
        "APP_URL",
        "APP_VERSION_REF"
      ]
    },
    "preview_gate": {
      "bootstrap": "cd apps/app-web && bun install --frozen-lockfile && bunx playwright install --with-deps chromium",
      "run": "uv run python tools/health_check.py \\"$APP_URL/api/health\\" \\"$APP_VERSION_REF\\" 36 && bash tools/warm_surface.sh \\"$APP_URL\\" / /research/rankings && (cd apps/app-web && TA_BASE_URL=\\"$APP_URL\\" node e2e/walk-tree.mjs) && uv run python tools/surface_contract.py --authenticated \\"$APP_URL\\"",
      "timeout_seconds": 360,
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
      "timeout_seconds": 420,
      "required_env": [
        "APP_URL",
        "APP_VERSION_REF",
        "TA_EMAIL",
        "TA_MEMBER_EMAIL",
        "TA_PASSWORD",
        "TA_MCP_TOKEN"
      ]
    },
    "prod_gate": {
      "run": "uv run python tools/surface_contract.py \\"$APP_URL\\"",
      "timeout_seconds": 240,
      "required_env": [
        "APP_URL"
      ]
    },
    "scheduled_audit": {
      "bootstrap": "uv sync --frozen",
      "run": "uv run python tools/nightly_verdicts.py \\"$APP_URL/api/health\\" --environment \\"$ENVIRONMENT\\" --repo . && uv run python tools/surface_contract.py \\"$APP_URL\\"",
      "timeout_seconds": 600,
      "checkout_lineage": true,
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
    assert hook_results[0]["cwd"] != str(fr_sub)
    assert hook_results[1]["cwd"] != str(ta_sub)


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
    assert ta_api.issues[0]["title"] == "app-hook is red: truealpha/app:smoke:staging"

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
    deploy step, and Alert on failure skips the page (dedup_page) when a failure occurs
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
    with pytest.raises(SystemExit) as exc_info:
        exec(compile(py_body, "<alert-on-failure>", "exec"), {})
    assert exc_info.value.code == 0  # the page went out through dedup_page
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
            "schema_version": 2,
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
            "schema_version": 2,
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
        "prod_gate",
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


def test_reconcile_rejects_off_main_app_release_tag_in_sync_and_deploy(
    monkeypatch, tmp_path
) -> None:
    """#1500 (AC2): both sync_app_releases (when applying/creating PR) and reconcile.main
    (when executing App deploy commands) must verify App version_ref is on main and fail
    closed when assert_app_version_ref_on_main rejects an off-main tag."""
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
            return (SHA, {"v0.2.46": "1" * 40, "v0.2.47": "bad" + "0" * 37})
        return ("2" * 40, {"v0.2.80": "2" * 40})

    monkeypatch.setattr(reconcile, "_query_remote_app_refs", _fake_remote)

    def _reject_off_main(version_ref, deploy_type, *, service, **_kw):
        raise ValueError(
            f"{service} version_ref {version_ref!r} is not reachable from main"
        )

    monkeypatch.setattr(reconcile, "assert_app_version_ref_on_main", _reject_off_main)

    # 1. sync_app_releases fails closed before mutating repos.yaml
    with pytest.raises(ValueError, match="not reachable from main"):
        reconcile.sync_app_releases(
            tmp_path,
            apply_repos_yaml=True,
        )

    # 2. main() non-dry-run deploy fails closed before running deploy_v2
    monkeypatch.setattr(reconcile, "assert_after_on_main", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        reconcile,
        "resolve_image_ref",
        lambda ref, **_kw: reconcile.ResolvedRef(sha=SHA, image_ref=ref, form="tag"),
    )
    with pytest.raises(SystemExit, match="not reachable from main"):
        main(
            [
                "--repo-root",
                str(tmp_path),
                "--after",
                "v3.1.25",
                "--changed-file",
                "harness/repos.yaml",
            ]
        )


def test_local_service_hook_manifests_discovery_and_execution(monkeypatch) -> None:
    """#1521: LOCAL_SERVICE_HOOK_MANIFESTS is dynamically derived from service_attrs() and
    both run_post_deploy_app_hooks and run_scheduled_app_audits execute platform/todo's
    runtime-hooks.manifest.json slots."""
    from infra2_sdk.manifests import HookRunResult

    assert reconcile.LOCAL_SERVICE_HOOK_MANIFESTS == {
        "platform/todo": "platform/30.todo/runtime-hooks.manifest.json",
    }

    executed: list[tuple[str, str, str, str, Path]] = []

    def _fake_exec(
        manifest,
        slot: str,
        *,
        app_url: str,
        environment: str,
        version_ref: str,
        cwd: Path,
        **_kw,
    ):
        executed.append((manifest.service, slot, app_url, environment, Path(cwd)))
        return HookRunResult(
            service=manifest.service,
            slot=slot,
            environment=environment,
            app_url=app_url,
            version_ref=version_ref,
            status="pass",
            exit_code=0,
            duration_seconds=0.05,
        )

    monkeypatch.setattr(reconcile, "execute_app_hook_slot", _fake_exec)

    plan = build_plan(["platform/30.todo/app.py"])
    applied = [
        reconcile.DeployCommand(
            service="platform/todo", deploy_type="staging", argv=["true"]
        )
    ]
    post_results = reconcile.run_post_deploy_app_hooks(
        plan,
        applied,
        repo_root=ROOT,
        after="v3.2.0",
        target_env="staging",
        slot="smoke,staging_gate",
        record_issues=False,
    )
    assert [r["slot"] for r in post_results] == ["smoke", "staging_gate"]
    assert all(
        r["service"] == "platform/todo" and r["ok"] is True for r in post_results
    )
    assert executed == [
        (
            "platform/todo",
            "smoke",
            "https://todo-staging.zitian.party",
            "staging",
            ROOT / "platform/30.todo",
        ),
        (
            "platform/todo",
            "staging_gate",
            "https://todo-staging.zitian.party",
            "staging",
            ROOT / "platform/30.todo",
        ),
    ]

    executed.clear()
    audit_results = reconcile.run_scheduled_app_audits(
        ROOT,
        target_env="prod",
        slot="scheduled_audit",
        record_trail=False,
        services=["platform/todo"],
    )
    assert len(audit_results) == 1
    assert audit_results[0]["service"] == "platform/todo"
    assert audit_results[0]["ok"] is True
    assert executed == [
        (
            "platform/todo",
            "scheduled_audit",
            "https://todo.zitian.party",
            "production",
            ROOT / "platform/30.todo",
        ),
    ]


def test_reconcile_workflow_always_runs_gates_and_every_sync_exits_after_its_pr() -> (
    None
):
    """#1521, #1590, #1628: no dispatch input skips the post-deploy gates, and a sync
    (schedule or dispatch) opens or updates its PR and exits 0 without deploying."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert "run_post_deploy_hooks" not in inputs
    script = next(
        step["run"]
        for step in workflow["jobs"]["reconcile"]["steps"]
        if step.get("name") == "Reconcile changed IaC inputs"
    )
    assert "run_post_deploy_hooks" not in script
    # the flag is deleted: hooks always run, so no script passes or tests it (#1628)
    assert "--run-post-deploy-hooks" not in script
    with pytest.raises(SystemExit) as refused:
        reconcile.main(["--run-post-deploy-hooks"])
    assert refused.value.code == 2  # argparse: unrecognized argument
    sched_idx = script.index(
        'if [[ "$EVENT_NAME" == "schedule" || "${{ inputs.sync_app_releases || false }}" == "true" ]]; then'
    )
    sched_block = script[sched_idx : script.index("RECONCILE_PHASE=reconcile-deploy")]
    assert "--sync-app-releases --apply-repos-yaml --sync-gitlinks" in sched_block
    assert "sync_args+=(--create-pr)" in sched_block
    assert "promote_prod" not in sched_block
    # the last fragment is the `echo "` that opens the deploy phase
    body = [line.strip() for line in sched_block.splitlines() if line.strip()][:-1]
    assert body[-2:] == ["exit 0", "fi"]
    assert "synced_infra_tag" not in script


def test_main_run_hook_slot_resolves_local_service_manifest_and_url(
    monkeypatch,
) -> None:
    """#1521: main(['--run-hook-slot', 'staging_gate', '--hook-service', 'platform/todo', ...])
    resolves platform/30.todo/runtime-hooks.manifest.json and https://todo-staging.zitian.party
    without raising ValueError: unknown app service 'platform/todo'."""
    from infra2_sdk.manifests import HookRunResult

    captured: list[tuple[str, str, str, str, str, Path]] = []

    def _fake_exec(
        manifest,
        slot: str,
        *,
        app_url: str,
        environment: str,
        version_ref: str,
        cwd: Path,
        **_kw,
    ):
        captured.append(
            (
                manifest.service,
                slot,
                app_url,
                environment,
                version_ref,
                Path(cwd),
            )
        )
        return HookRunResult(
            service=manifest.service,
            slot=slot,
            environment=environment,
            app_url=app_url,
            version_ref=version_ref,
            status="pass",
            exit_code=0,
            duration_seconds=0.05,
        )

    monkeypatch.setattr(reconcile, "execute_app_hook_slot", _fake_exec)

    rc = main(
        [
            "--repo-root",
            str(ROOT),
            "--run-hook-slot",
            "staging_gate",
            "--hook-service",
            "platform/todo",
            "--domain",
            "zitian.party",
            "--version-ref",
            "v3.1.35",
        ]
    )
    assert rc == 0
    assert captured == [
        (
            "platform/todo",
            "staging_gate",
            "https://todo-staging.zitian.party",
            "staging",
            "v3.1.35",
            ROOT / "platform/30.todo",
        )
    ]


# --- #1529: the env that the hook runner supplies to App hook slots ----------------------

_HOOK_SLOT_FLAGS = (
    "--run-hook-slot",
    "--run-scheduled-audit",
)


def _secret_name(value: object) -> str | None:
    """Return NAME when ``value`` is exactly the expression ``${{ secrets.NAME }}``."""
    match = re.fullmatch(
        r"\$\{\{\s*secrets\.([A-Za-z0-9_]+)\s*\}\}", str(value).strip()
    )
    return match.group(1) if match else None


def _hook_slot_steps(
    workflows: dict[str, dict],
) -> list[tuple[str, str, str, dict]]:
    """Return (file, job, step name, effective env) of each step that runs an App hook slot.

    The effective env is the workflow env, overlaid by the job env, overlaid by the step env.
    """
    found: list[tuple[str, str, str, dict]] = []
    for file_name, workflow in workflows.items():
        for job_id, job in (workflow.get("jobs") or {}).items():
            for index, step in enumerate(job.get("steps") or []):
                script = step.get("run")
                if not isinstance(script, str) or not any(
                    flag in script for flag in _HOOK_SLOT_FLAGS
                ):
                    continue
                env = {
                    **(workflow.get("env") or {}),
                    **(job.get("env") or {}),
                    **(step.get("env") or {}),
                }
                found.append((file_name, job_id, step.get("name") or f"#{index}", env))
    return found


def _unsupplied_runner_env(
    workflows: dict[str, dict], table: dict[str, str]
) -> list[str]:
    """Return one line per hook-slot step that lacks a table name or uses the wrong secret."""
    problems: list[str] = []
    for file_name, job_id, step_name, env in _hook_slot_steps(workflows):
        for name, secret in table.items():
            if _secret_name(env.get(name, "")) != secret:
                problems.append(
                    f"{file_name} job {job_id!r} step {step_name!r}: "
                    f"{name} must be ${{{{ secrets.{secret} }}}}, got {env.get(name)!r}"
                )
    return problems


def _load_workflows() -> dict[str, dict]:
    return {
        path.name: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
    }


def test_workflow_steps_that_run_hook_slots_supply_every_runner_secret_env() -> None:
    """#1529: each step that runs an App hook slot gets each name of
    HOOK_RUNNER_SECRET_ENV from the secret that the table names. Run 42809042184 failed with
    ``missing required hook env: AI_API_KEY`` because reconcile-iac-inputs.yml did not."""
    from common.deploy import app_contract

    table = dict(app_contract.HOOK_RUNNER_SECRET_ENV)
    workflows = _load_workflows()

    # The scan must find both known runners. An empty scan would pass without checking.
    runners = {(file_name, job) for file_name, job, _, _ in _hook_slot_steps(workflows)}
    assert {
        ("reconcile-iac-inputs.yml", "reconcile"),
        ("ops-checks.yml", "audit"),
    } <= runners
    assert table
    assert _unsupplied_runner_env(workflows, table) == []

    # Mutation proof 1: a job env without the name fails.
    mutated = yaml.safe_load(yaml.safe_dump(workflows))
    del mutated["reconcile-iac-inputs.yml"]["jobs"]["reconcile"]["env"]["AI_API_KEY"]
    problems = _unsupplied_runner_env(mutated, table)
    assert len(problems) == 1
    assert "reconcile-iac-inputs.yml job 'reconcile'" in problems[0]
    assert "AI_API_KEY must be ${{ secrets.ZAI_CODING_CN_API_KEY }}" in problems[0]

    # Mutation proof 2: the name from another secret fails.
    mutated = yaml.safe_load(yaml.safe_dump(workflows))
    mutated["reconcile-iac-inputs.yml"]["jobs"]["reconcile"]["env"]["AI_API_KEY"] = (
        "${{ secrets.OTHER_API_KEY }}"
    )
    assert len(_unsupplied_runner_env(mutated, table)) == 1


# --- #1603: the bun that App hook commands call must exist on the hook runner -------------

_BUN_COMMAND = re.compile(r"\bbunx?\b")
_EXACT_BUN_VERSION = re.compile(r"\d+\.\d+\.\d+")
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
_SCHEDULED_AUDIT_FLAG = "--run-scheduled-audit"


def _bun_slots(raw_manifests: tuple[str, ...]) -> list[tuple[str, str, str, str]]:
    """Return (service, slot, field, command) for each bootstrap or run command of a slot
    that calls ``bun`` or ``bunx``."""
    found: list[tuple[str, str, str, str]] = []
    for raw_manifest in raw_manifests:
        manifest = json.loads(raw_manifest)
        for slot, spec in manifest["slots"].items():
            for field in ("bootstrap", "run"):
                command = spec.get(field) or ""
                if _BUN_COMMAND.search(command):
                    found.append((manifest["service"], slot, field, command))
    return found


def _setup_bun_ref(step: dict) -> str | None:
    """Return the ref after ``@`` when the step uses oven-sh/setup-bun, else None."""
    uses = step.get("uses")
    if isinstance(uses, str) and uses.startswith("oven-sh/setup-bun@"):
        return uses.split("@", 1)[1]
    return None


def _is_pinned_setup_bun(step: dict) -> bool:
    """Return True when the step pins setup-bun to a 40-hex commit SHA and bun to x.y.z.

    The job env holds secrets. A movable tag of a third-party action is a supply-chain path.
    """
    ref = _setup_bun_ref(step)
    version = (step.get("with") or {}).get("bun-version")
    return (
        ref is not None
        and _COMMIT_SHA.fullmatch(ref) is not None
        and _EXACT_BUN_VERSION.fullmatch(str(version)) is not None
    )


def _hook_steps_without_bun(
    workflows: dict[str, dict], raw_manifests: tuple[str, ...]
) -> list[str]:
    """Return one line per hook-slot step that can reach a slot whose command calls bun
    and has no earlier step of the same job that installs bun at an exact version with a
    setup action pinned to a commit SHA.

    A step that runs only ``--run-scheduled-audit`` reaches only ``scheduled_audit`` slots.
    Every other hook-slot step can reach every slot.
    """
    bun_slots = _bun_slots(raw_manifests)
    problems: list[str] = []
    for file_name, job_id, step_name, _env in _hook_slot_steps(workflows):
        steps = workflows[file_name]["jobs"][job_id]["steps"]
        index = next(
            i
            for i, step in enumerate(steps)
            if (step.get("name") or f"#{i}") == step_name
            and any(flag in str(step.get("run")) for flag in _HOOK_SLOT_FLAGS)
        )
        script = steps[index]["run"]
        audit_only = _SCHEDULED_AUDIT_FLAG in script and not any(
            flag in script for flag in _HOOK_SLOT_FLAGS if flag != _SCHEDULED_AUDIT_FLAG
        )
        reached = [
            (service, slot, field, command)
            for service, slot, field, command in bun_slots
            if not audit_only or slot == "scheduled_audit"
        ]
        if not reached or any(_is_pinned_setup_bun(step) for step in steps[:index]):
            continue
        calls = "; ".join(
            f"{service} slot {slot!r} {field} {command!r}"
            for service, slot, field, command in reached
        )
        seen = [
            f"ref {_setup_bun_ref(step)!r} "
            f"bun-version {(step.get('with') or {}).get('bun-version')!r}"
            for step in steps[:index]
            if _setup_bun_ref(step) is not None
        ]
        problems.append(
            f"{file_name} job {job_id!r} step {step_name!r} runs App hook slots, and "
            f"these commands call bun: {calls}. Missing step: an earlier step of the job "
            f"that uses oven-sh/setup-bun@<40-hex commit SHA> with bun-version: <x.y.z>. "
            f"Setup steps found: {seen or 'none'}"
        )
    return problems


def test_hook_slot_steps_install_the_bun_that_manifest_commands_call() -> None:
    """#1603: run 42982038433 failed the truealpha staging_gate bootstrap with
    ``sh: 1: bun: not found``, because the reconcile job installed Python and uv only.
    Each step that can run a slot calling bun or bunx needs an earlier step of the same job
    that installs bun at an exact version. The job env holds secrets, so the setup action
    is pinned to a 40-hex commit SHA, not to a movable tag."""
    raw_manifests = (_REAL_TA_HOOKS_MANIFEST_JSON, _REAL_FR_HOOKS_MANIFEST_JSON)
    workflows = _load_workflows()

    # The scan must find the known bun slots. An empty scan would pass without checking.
    found = {(service, slot) for service, slot, _, _ in _bun_slots(raw_manifests)}
    assert {
        ("truealpha/app", "preview_gate"),
        ("truealpha/app", "staging_gate"),
    } <= found
    runners = {(file_name, job) for file_name, job, _, _ in _hook_slot_steps(workflows)}
    assert {
        ("reconcile-iac-inputs.yml", "reconcile"),
        ("ops-checks.yml", "audit"),
    } <= runners
    assert _hook_steps_without_bun(workflows, raw_manifests) == []

    def mutated_copy() -> tuple[dict[str, dict], list[dict]]:
        copy = yaml.safe_load(yaml.safe_dump(workflows))
        return copy, copy["reconcile-iac-inputs.yml"]["jobs"]["reconcile"]["steps"]

    # Mutation proof 1: a job without the setup step gives exactly one problem.
    mutated, steps = mutated_copy()
    kept = [s for s in steps if not str(s.get("uses")).startswith("oven-sh/setup-bun@")]
    assert len(steps) - len(kept) == 1
    steps[:] = kept
    problems = _hook_steps_without_bun(mutated, raw_manifests)
    assert len(problems) == 1
    assert "reconcile-iac-inputs.yml job 'reconcile'" in problems[0]
    assert "step 'Reconcile changed IaC inputs'" in problems[0]
    assert "truealpha/app slot 'staging_gate' bootstrap" in problems[0]
    assert "truealpha/app slot 'preview_gate' bootstrap" in problems[0]
    assert "bun install --frozen-lockfile" in problems[0]
    assert (
        "oven-sh/setup-bun@<40-hex commit SHA> with bun-version: <x.y.z>" in problems[0]
    )

    # Mutation proof 2: a version that is not exact fails.
    for version in ("latest", "1.4", "1.x", "", None):
        mutated, steps = mutated_copy()
        (setup,) = [
            s for s in steps if str(s.get("uses")).startswith("oven-sh/setup-bun@")
        ]
        if version is None:
            del setup["with"]["bun-version"]
        else:
            setup["with"]["bun-version"] = version
        problems = _hook_steps_without_bun(mutated, raw_manifests)
        assert len(problems) == 1, version
        assert "oven-sh/setup-bun@" in problems[0], version

    # Mutation proof 3: a setup step after the hook step fails.
    mutated, steps = mutated_copy()
    (setup,) = [s for s in steps if str(s.get("uses")).startswith("oven-sh/setup-bun@")]
    steps.remove(setup)
    steps.append(setup)
    assert len(_hook_steps_without_bun(mutated, raw_manifests)) == 1

    # Mutation proof 4: a bun call in a scheduled_audit slot reaches the audit job only.
    ops_manifest = json.loads(_REAL_TA_HOOKS_MANIFEST_JSON)
    ops_manifest["slots"]["scheduled_audit"]["bootstrap"] = (
        "bun install --frozen-lockfile"
    )
    problems = _hook_steps_without_bun(workflows, (json.dumps(ops_manifest),))
    assert len(problems) == 1
    assert "ops-checks.yml job 'audit'" in problems[0]
    assert (
        "slot 'scheduled_audit' bootstrap 'bun install --frozen-lockfile'"
        in problems[0]
    )

    # Mutation proof 5: a ref that is not a 40-hex commit SHA fails. A 40-hex SHA passes.
    mutated, steps = mutated_copy()
    (setup,) = [s for s in steps if str(s.get("uses")).startswith("oven-sh/setup-bun@")]
    real_ref = _setup_bun_ref(setup)
    assert real_ref is not None and _COMMIT_SHA.fullmatch(real_ref)
    for ref in ("v2.2.0", "v2", "main", real_ref[:39], real_ref + "0"):
        mutated, steps = mutated_copy()
        (setup,) = [
            s for s in steps if str(s.get("uses")).startswith("oven-sh/setup-bun@")
        ]
        setup["uses"] = f"oven-sh/setup-bun@{ref}"
        problems = _hook_steps_without_bun(mutated, raw_manifests)
        assert len(problems) == 1, ref
        assert f"ref {ref!r} bun-version '1.4.4'" in problems[0], ref
    mutated, steps = mutated_copy()
    (setup,) = [s for s in steps if str(s.get("uses")).startswith("oven-sh/setup-bun@")]
    setup["uses"] = "oven-sh/setup-bun@" + "0123456789abcdef" * 2 + "01234567"
    assert _hook_steps_without_bun(mutated, raw_manifests) == []

    # Mutation proof 6: the word match finds bun and bunx, and no other word.
    word_manifest = json.loads(_REAL_FR_HOOKS_MANIFEST_JSON)
    word_manifest["slots"]["smoke"]["run"] = "bash debunk.sh && bundle exec bunker"
    assert _bun_slots((json.dumps(word_manifest),)) == []
    word_manifest["slots"]["smoke"]["run"] = "cd web && bunx playwright test"
    assert [slot for _, slot, _, _ in _bun_slots((json.dumps(word_manifest),))] == [
        "smoke"
    ]


def _required_env_without_source(raw_manifest: dict) -> dict[str, tuple[str, ...]]:
    """Map each slot to its ``required_env`` names that the infra2 runner never supplies.

    The runner supplies the names of HOOK_RUNNER_SECRET_ENV (from a secret) and of
    HOOK_RUNNER_CONTEXT_ENV (from the deploy request).
    """
    from common.deploy import app_contract

    manifest = reconcile.RuntimeHooksManifest.from_dict(raw_manifest)
    supplied = set(app_contract.HOOK_RUNNER_SECRET_ENV) | set(
        app_contract.HOOK_RUNNER_CONTEXT_ENV
    )
    gaps = {
        slot: tuple(name for name in spec.required_env if name not in supplied)
        for slot, spec in manifest.slots.items()
    }
    return {slot: names for slot, names in gaps.items() if names}


@pytest.mark.parametrize(
    "raw_manifest",
    [
        pytest.param(_REAL_FR_HOOKS_MANIFEST_JSON, id="finance_report"),
        pytest.param(_REAL_TA_HOOKS_MANIFEST_JSON, id="truealpha"),
    ],
)
def test_app_hook_manifest_required_env_has_an_infra2_source(raw_manifest: str) -> None:
    """#1529: every ``required_env`` name of every slot has a source in the infra2 runner."""
    assert _required_env_without_source(json.loads(raw_manifest)) == {}


def test_required_env_check_names_an_unknown_required_env() -> None:
    """#1529: the check returns the slot and name of a ``required_env`` entry without source."""
    manifest = json.loads(_REAL_FR_HOOKS_MANIFEST_JSON)
    manifest["slots"]["staging_gate"].setdefault("required_env", []).append(
        "NEW_REQUIRED_NAME"
    )
    assert _required_env_without_source(manifest) == {
        "staging_gate": ("NEW_REQUIRED_NAME",)
    }


def test_local_service_hook_manifests_required_env_has_an_infra2_source() -> None:
    """#1529: the local manifests that the runner executes need no env beyond the runner's."""
    paths = sorted(reconcile.LOCAL_SERVICE_HOOK_MANIFESTS.values())
    assert paths
    gaps = {
        rel: _required_env_without_source(
            json.loads((ROOT / rel).read_text(encoding="utf-8"))
        )
        for rel in paths
    }
    assert gaps == {rel: {} for rel in paths}


def test_hook_runner_context_env_names_reach_a_slot_without_ambient_values(
    tmp_path,
) -> None:
    """#1529: execute_app_hook_slot supplies each name of HOOK_RUNNER_CONTEXT_ENV when the
    ambient env holds none of them. It rejects a ``required_env`` name outside that set,
    and a name that it adds to ``optional_env`` itself."""
    from common.deploy import app_contract
    from infra2_sdk.manifests import HOOK_SLOTS

    def _manifest(required: list[str]) -> reconcile.RuntimeHooksManifest:
        return reconcile.RuntimeHooksManifest.from_dict(
            {
                "schema_version": 2,
                "service": "finance_report/app",
                "slots": {
                    slot: {
                        "run": "echo ok",
                        "timeout_seconds": 30,
                        "required_env": required if slot == "smoke" else [],
                    }
                    for slot in HOOK_SLOTS
                },
            }
        )

    seen: list[dict[str, str]] = []

    def _runner(cmd, *, env=None, **_kw):
        seen.append(dict(env or {}))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    context_names = sorted(app_contract.HOOK_RUNNER_CONTEXT_ENV)
    assert context_names
    result = app_contract.execute_app_hook_slot(
        _manifest(context_names),
        "smoke",
        app_url="https://app.example.invalid",
        environment="staging",
        version_ref="v1.2.3",
        cwd=tmp_path,
        ambient_env={"PATH": "/usr/bin:/bin"},
        runner=_runner,
    )
    assert result.status == "pass"
    assert {name: bool(seen[-1].get(name)) for name in context_names} == {
        name: True for name in context_names
    }

    rejected = {
        "NEW_REQUIRED_NAME": "missing required hook env: NEW_REQUIRED_NAME",
        # The runner lists these three in optional_env of every slot.
        "INFRA2_HOOK_APP_URL": "cannot be in both required_env and optional_env",
        "BASE_URL": "cannot be in both required_env and optional_env",
        "DEPLOY_ENV": "cannot be in both required_env and optional_env",
    }
    for name, message in rejected.items():
        assert name not in app_contract.HOOK_RUNNER_CONTEXT_ENV
        with pytest.raises(ValueError, match=message):
            app_contract.execute_app_hook_slot(
                _manifest([name]),
                "smoke",
                app_url="https://app.example.invalid",
                environment="staging",
                version_ref="v1.2.3",
                cwd=tmp_path,
                ambient_env={"PATH": "/usr/bin:/bin"},
                runner=_runner,
            )


def test_run_post_deploy_app_hooks_records_failed_entry_on_missing_required_hook_env(
    monkeypatch,
) -> None:
    """#1533: when a hook slot raises ValueError('missing required hook env: ...'),
    run_post_deploy_app_hooks records a failed HookRunResult (status='fail', ok=False,
    exit_code=1) instead of an unhandled traceback, and judge_staging_gate rejects it."""
    from tools.promotion_soak_guard import judge_staging_gate

    def _fake_exec(
        manifest,
        slot: str,
        **_kw,
    ):
        raise ValueError("missing required hook env: ['AI_API_KEY']")

    monkeypatch.setattr(reconcile, "execute_app_hook_slot", _fake_exec)

    plan = build_plan(["platform/30.todo/app.py"])
    applied = [
        reconcile.DeployCommand(
            service="platform/todo", deploy_type="staging", argv=["true"]
        )
    ]
    post_results = reconcile.run_post_deploy_app_hooks(
        plan,
        applied,
        repo_root=ROOT,
        after="v3.2.0",
        target_env="staging",
        slot="staging_gate",
        record_issues=False,
    )
    assert len(post_results) == 1
    assert post_results[0]["service"] == "platform/todo"
    assert post_results[0]["slot"] == "staging_gate"
    assert post_results[0]["status"] == "fail"
    assert post_results[0]["ok"] is False
    assert post_results[0]["exit_code"] == 1
    assert "missing required hook env" in str(post_results[0]["missing_env_error"])

    ok, detail = judge_staging_gate(post_results, {"platform/todo": "v3.2.0"})
    assert ok is False
    assert detail == "failed staging_gate for platform/todo@v3.2.0"


def test_stage_hook_plans_ssot_and_workflow_wiring() -> None:
    """#1608: DEFAULT_STAGE_HOOK_SLOTS / default_hook_slot_for_stage are deleted in favor of
    infra2_sdk.manifests.STAGE_HOOK_PLANS / stage_hook_plan (Pre -> Deploy smoke -> Post gate),
    and reconcile-iac-inputs.yml delegates slot selection to stage_hook_plan."""
    from infra2_sdk.manifests import HOOK_SLOTS, STAGE_HOOK_PLANS, stage_hook_plan

    assert not hasattr(reconcile, "DEFAULT_STAGE_HOOK_SLOTS")
    assert not hasattr(reconcile, "default_hook_slot_for_stage")
    assert reconcile.STAGE_HOOK_PLANS is STAGE_HOOK_PLANS
    assert reconcile.stage_hook_plan is stage_hook_plan

    covered_slots = {
        slot
        for plan in STAGE_HOOK_PLANS.values()
        for slot in (plan.operation_slot, plan.post_slot)
        if slot
    }
    assert covered_slots == set(HOOK_SLOTS)
    assert reconcile._stage_slots_csv("staging") == "smoke,staging_gate"
    assert reconcile._stage_slots_csv("prod") == "smoke,prod_gate"
    assert reconcile._stage_slots_csv("production") == "smoke,prod_gate"
    assert reconcile._stage_slots_csv("preview") == "smoke,preview_gate"
    assert reconcile._stage_slots_csv("schedule") == "scheduled_audit"
    with pytest.raises(ValueError, match="unknown"):
        reconcile._stage_slots_csv("unknown")

    workflow_text = WORKFLOW.read_text(encoding="utf-8")
    assert "--post-deploy-hook-slot" not in workflow_text
    assert "--run-post-deploy-hooks" not in workflow_text
    assert '[[ -n "$INPUT_AFTER" ]] && hook_args+=(--version-ref "$INPUT_AFTER")' in (
        workflow_text
    )
    assert '[[ -n "$after" ]] && hook_args+=(--version-ref "$after")' not in (
        workflow_text
    )


def test_sync_app_releases_cut_missing_app_tag_runs_ephemeral_preview_gate_and_always_tears_down(
    monkeypatch, tmp_path
) -> None:
    """#1554: when cut_missing_app_tag=True and an App's main is ahead of its latest tag,
    sync_app_releases deploys preview/branch (main), runs preview_gate, cuts and pushes
    refs/tags/vX.Y.Z only on pass, and always runs deploy_v2 --down in finally."""
    from infra2_sdk.manifests import HOOK_SLOTS, HookRunResult, RuntimeHooksManifest

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
            return (SHA, {"v0.2.46": "1" * 40})
        return ("2" * 40, {"v0.2.80": "2" * 40})

    monkeypatch.setattr(reconcile, "_query_remote_app_refs", _fake_remote)
    monkeypatch.setattr(reconcile, "newest_release_tag", lambda **_kw: "v3.1.40")

    manifest = RuntimeHooksManifest.from_dict(
        {
            "schema_version": 2,
            "service": "finance_report/app",
            "slots": {
                slot_name: {
                    "run": "echo ok",
                    "bootstrap": "",
                    "timeout_seconds": 30,
                    "required_env": [],
                    "optional_env": [],
                }
                for slot_name in HOOK_SLOTS
            },
        }
    )
    monkeypatch.setattr(reconcile, "fetch_hooks_manifest", lambda *_a, **_kw: manifest)
    monkeypatch.setattr(
        reconcile,
        "resolve_app_hook_cwd",
        lambda *_a, **_kw: (tmp_path, None),
    )

    commands_seen: list[list[str]] = []
    hook_calls: list[tuple[str, str, str, str]] = []
    gate_should_pass = True

    def _fake_exec(
        _m, slot: str, *, app_url: str, environment: str, version_ref: str, **_kw
    ):
        hook_calls.append((slot, app_url, environment, version_ref))
        return HookRunResult(
            service="finance_report/app",
            slot=slot,
            environment=environment,
            app_url=app_url,
            version_ref=version_ref,
            status="pass" if gate_should_pass else "fail",
            exit_code=0 if gate_should_pass else 1,
            duration_seconds=0.2,
            summary="ok" if gate_should_pass else "preview e2e failed",
        )

    monkeypatch.setattr(reconcile, "execute_app_hook_slot", _fake_exec)

    def _runner(cmd, **_kw):
        argv = [str(x) for x in cmd]
        commands_seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    # 1. Passing preview_gate: deploys up, runs preview_gate, pushes tag ref, tears down (--down).
    res = reconcile.sync_app_releases(
        tmp_path,
        apply_repos_yaml=True,
        cut_missing_app_tag=True,
        runner=_runner,
    )
    assert res["updates"] == {"finance_report/app": "v0.2.47"}
    assert res["apps"]["finance_report/app"]["preview_verified"] is True
    assert (
        res["apps"]["finance_report/app"]["preview_url"]
        == "https://report-branch-main.zitian.party"
    )
    assert hook_calls == [
        (
            "preview_gate",
            "https://report-branch-main.zitian.party",
            "preview",
            SHA[:7],
        )
    ]
    deploy_v2_calls = [c for c in commands_seen if "tools.deploy_v2" in c]
    assert len(deploy_v2_calls) == 2
    assert "--down" not in deploy_v2_calls[0]
    assert "preview/branch" in deploy_v2_calls[0]
    assert "--down" in deploy_v2_calls[1]
    assert [
        "gh",
        "api",
        "repos/wangzitian0/finance_report/git/refs",
        "-f",
        "ref=refs/tags/v0.2.47",
        "-f",
        f"sha={SHA}",
    ] in commands_seen

    # 2. Failing preview_gate (mutation proof): tears down (--down) in finally, never pushes tag, raises RuntimeError.
    commands_seen.clear()
    hook_calls.clear()
    gate_should_pass = False
    with pytest.raises(RuntimeError, match="preview_gate failed"):
        reconcile.sync_app_releases(
            tmp_path,
            apply_repos_yaml=True,
            cut_missing_app_tag=True,
            runner=_runner,
        )
    deploy_v2_calls_fail = [c for c in commands_seen if "tools.deploy_v2" in c]
    assert len(deploy_v2_calls_fail) == 2
    assert "--down" in deploy_v2_calls_fail[1]
    assert not any(c[:2] == ["gh", "api"] for c in commands_seen)


def test_audit_app_hooks_fails_closed_on_unsupplied_required_env(
    monkeypatch, capsys, tmp_path
) -> None:
    """#1534: audit_app_hooks rejects any slot whose required_env lacks an infra2 source in
    HOOK_RUNNER_SECRET_ENV | HOOK_RUNNER_CONTEXT_ENV instead of masking it with a dry-run placeholder."""
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

    bad_ta = json.loads(_REAL_TA_HOOKS_MANIFEST_JSON)
    bad_ta["slots"]["staging_gate"]["required_env"].append("UNSUPPLIED_TA_SECRET")
    bad_fr = json.loads(_REAL_FR_HOOKS_MANIFEST_JSON)
    bad_fr["slots"]["smoke"]["required_env"] = ["UNSUPPLIED_SMOKE_SECRET"]

    monkeypatch.setattr(
        reconcile,
        "fetch_hooks_manifest",
        lambda svc, _sha, **_kw: reconcile.RuntimeHooksManifest.from_dict(
            bad_fr if svc == "finance_report/app" else bad_ta
        ),
    )

    rc = main(["--repo-root", str(tmp_path), "--audit-app-hooks"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert (
        "truealpha/app: unsupplied required_env 'UNSUPPLIED_TA_SECRET' in slot 'staging_gate'"
        in out["errors"]
    )
    assert (
        "finance_report/app: unsupplied required_env 'UNSUPPLIED_SMOKE_SECRET' in slot 'smoke'"
        in out["errors"]
    )
    assert any(
        "finance_report/app: smoke: missing required hook env" in err
        for err in out["errors"]
    )


def test_failure_alert_text_classifies_deploy_failure_hook_failure_and_success() -> (
    None
):
    """#1577: failure_alert_text classifies three states: (1) container deploy failed,
    (2) container deploys succeeded and post-deploy hook failed, and (3) all passed."""
    deploy_ok = [{"service": "truealpha/app", "type": "staging", "returncode": 0}]
    deploy_fail = [
        {
            "service": "truealpha/app",
            "type": "staging",
            "returncode": 1,
            "stderr": "deploy_v2 failed: runner timeout\n",
        }
    ]
    hook_fail = [
        {
            "service": "truealpha/app",
            "slot": "staging_gate",
            "version_ref": "v0.2.85",
            "status": "fail",
            "summary": "missing required hook env: TA_EMAIL\nextra line ignored",
            "stderr_tail": "stderr fallback",
        }
    ]
    hook_pass = [
        {
            "service": "truealpha/app",
            "slot": "staging_gate",
            "version_ref": "v0.2.85",
            "status": "pass",
            "summary": "ok",
        }
    ]

    # State 1: deploy failed takes precedence even if hook_results also has entries
    state1 = reconcile.failure_alert_text(
        after="v3.1.45",
        results=deploy_fail,
        hook_results=hook_fail,
        run_url="https://x/runs/1",
        promote_prod=False,
    )
    assert state1 is not None
    assert state1.startswith("🚨 IaC reconcile FAILED for v3.1.45 (staging soak).")
    assert "Post-deploy hook FAILED" not in state1

    # State 2 (staging soak): deploys succeeded, post-deploy hook failed
    state2_staging = reconcile.failure_alert_text(
        after="v3.1.45",
        results=deploy_ok,
        hook_results=hook_fail,
        run_url="https://x/runs/2",
        promote_prod=False,
    )
    assert state2_staging == (
        "⚠️ Post-deploy hook FAILED for v3.1.45 (staging soak; container deploys succeeded).\n"
        "- truealpha/app [staging_gate @ v0.2.85]: missing required hook env: TA_EMAIL\n"
        "Run: https://x/runs/2"
    )

    # State 2 (prod promotion + blank summary falling back to stderr_tail)
    state2_prod = reconcile.failure_alert_text(
        after="v3.1.46",
        results=deploy_ok,
        hook_results=[
            {
                "service": "finance_report/app",
                "slot": "smoke",
                "version_ref": "",
                "status": "fail",
                "summary": "   ",
                "stderr_tail": "HTTP 502 Bad Gateway\ntraceback",
            }
        ],
        run_url="https://x/runs/3",
        promote_prod=True,
    )
    assert state2_prod == (
        "⚠️ Post-deploy hook FAILED for v3.1.46 (PRODUCTION promotion; container deploys succeeded).\n"
        "- finance_report/app [smoke @ v3.1.46]: HTTP 502 Bad Gateway\n"
        "Run: https://x/runs/3"
    )

    # State 3: neither deploy nor hook failed -> None
    assert (
        reconcile.failure_alert_text(
            after="v3.1.45",
            results=deploy_ok,
            hook_results=hook_pass,
            run_url="https://x/runs/4",
            promote_prod=False,
        )
        is None
    )


def test_record_app_hook_issue_trail_falls_back_to_infra2_under_app_hook_prefix() -> (
    None
):
    """#1590: cross-repo 403 falls back to `wangzitian0/infra2` using native
    `title_prefix='app-hook is red: '` with zero dict side-channel mutations."""
    from common.observability.issue_trail import WriteFailed

    class _FakeIssueApi:
        def __init__(self, repo: str, *, forbid_writes: bool = False) -> None:
            self.repo = repo
            self.forbid_writes = forbid_writes
            self.issues: list[dict] = []
            self.comments: list[tuple[int, str]] = []
            self.closed: list[int] = []

        def open_issues(self, *, with_body: bool = False) -> list[dict]:
            return list(self.issues)

        def create(self, title: str, body: str, labels) -> int:
            if self.forbid_writes:
                raise WriteFailed("HTTP 403 Resource not accessible by integration")
            num = 1572 if self.repo == "wangzitian0/infra2" else len(self.issues) + 10
            self.issues.append({"number": num, "title": title, "body": body})
            return num

        def comment(self, number: int, body: str) -> None:
            if self.forbid_writes:
                raise WriteFailed("HTTP 403 Resource not accessible by integration")
            self.comments.append((number, body))

        def close(self, number: int) -> None:
            if self.forbid_writes:
                raise WriteFailed("HTTP 403 Resource not accessible by integration")
            self.closed.append(number)

    apis = {
        "wangzitian0/truealpha": _FakeIssueApi(
            "wangzitian0/truealpha", forbid_writes=True
        ),
        "wangzitian0/infra2": _FakeIssueApi("wangzitian0/infra2"),
    }
    env = {
        "GITHUB_TOKEN": "tok-123",
        "GITHUB_EVENT_NAME": "schedule",
        "GITHUB_REPOSITORY": "wangzitian0/infra2",
    }

    fail_item = {
        "service": "truealpha/app",
        "slot": "staging_gate",
        "status": "fail",
        "version_ref": "v0.2.87",
        "summary": "missing required hook env",
    }
    assert (
        reconcile.record_app_hook_issue_trail(
            [fail_item],
            target_env="staging",
            slot="staging_gate",
            env=env,
            issues_factory=lambda repo, _tok: apis[repo],
        )
        == 0
    )
    assert "issue_trail_action" not in fail_item
    assert apis["wangzitian0/infra2"].issues == [
        {
            "number": 1572,
            "title": "app-hook is red: truealpha/app:staging_gate:staging",
            "body": apis["wangzitian0/infra2"].issues[0]["body"],
        }
    ]
    assert "app runtime hook" in apis["wangzitian0/infra2"].issues[0]["body"]

    # Repeat failure comments on #1572 instead of creating a duplicate
    assert (
        reconcile.record_app_hook_issue_trail(
            [dict(fail_item)],
            target_env="staging",
            slot="staging_gate",
            env=env,
            issues_factory=lambda repo, _tok: apis[repo],
        )
        == 0
    )
    assert len(apis["wangzitian0/infra2"].issues) == 1
    assert [n for n, _ in apis["wangzitian0/infra2"].comments] == [1572]

    # Passing run closes the fallback issue in wangzitian0/infra2
    assert (
        reconcile.record_app_hook_issue_trail(
            [
                {
                    "service": "truealpha/app",
                    "slot": "staging_gate",
                    "status": "pass",
                    "version_ref": "v0.2.87",
                    "summary": "passed",
                }
            ],
            target_env="staging",
            slot="staging_gate",
            env=env,
            issues_factory=lambda repo, _tok: apis[repo],
        )
        == 0
    )
    assert apis["wangzitian0/infra2"].closed == [1572]


def test_resolve_workflow_failure_alert_and_scheduled_audit_json_contract(
    monkeypatch, tmp_path, capsys
) -> None:
    """#1590: resolve_workflow_failure_alert fails closed on missing/corrupt/null JSON,
    skips sync-app-releases failures before deploy, and --run-scheduled-audit writes
    structured scheduled_audit_results for page_dedup."""
    from infra2_sdk.manifests import HOOK_SLOTS, HookRunResult, RuntimeHooksManifest
    from infra2_sdk.refs import ResolvedRef

    result_file = tmp_path / "reconcile-result.json"
    run_url = "https://github.com/wangzitian0/infra2/actions/runs/777"

    assert reconcile.resolve_workflow_failure_alert(
        result_file,
        after="",
        promote_prod=False,
        run_url=run_url,
        reconcile_phase="sync-app-releases",
    ) == (
        None,
        "::warning::App release PR sync failed before reconcile-deploy phase; skipping deploy-outage Feishu alert.",
    )

    expected_crash = (
        "🚨 IaC reconcile FAILED for v3.1.48 (staging soak) before any "
        f"deploy result was recorded (guard, provenance or runner unreachable).\nRun: {run_url}"
    )
    assert reconcile.resolve_workflow_failure_alert(
        result_file,
        after="v3.1.48",
        promote_prod=False,
        run_url=run_url,
        reconcile_phase="reconcile-deploy",
    ) == (expected_crash, None)

    result_file.write_text("{corrupt", encoding="utf-8")
    text_prod, notice_prod = reconcile.resolve_workflow_failure_alert(
        result_file, after="v3.1.48", promote_prod=True, run_url=run_url
    )
    assert notice_prod is None and "(PRODUCTION promotion)" in (text_prod or "")

    hook_msg = f"⚠️ Post-deploy hook FAILED for v3.1.48 (staging soak; container deploys succeeded).\nRun: {run_url}"
    result_file.write_text(json.dumps({"alert_text": hook_msg}), encoding="utf-8")
    assert reconcile.resolve_workflow_failure_alert(
        result_file, after="v3.1.48", promote_prod=False, run_url=run_url
    ) == (hook_msg, None)

    repos_yaml = tmp_path / "harness" / "repos.yaml"
    repos_yaml.parent.mkdir(parents=True, exist_ok=True)
    repos_yaml.write_text(
        "schema_version: 1\nrepositories:\n  - id: truealpha\n    role: external-application\n    release_tag: v0.2.87\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        reconcile,
        "resolve_image_ref",
        lambda ref, **_kw: ResolvedRef(sha=SHA, image_ref=ref, form="tag"),
    )
    manifest = RuntimeHooksManifest.from_dict(
        {
            "schema_version": 2,
            "service": "truealpha/app",
            "slots": {s: {"run": "exit 1", "timeout_seconds": 30} for s in HOOK_SLOTS},
        }
    )
    monkeypatch.setattr(reconcile, "fetch_hooks_manifest", lambda *_a, **_kw: manifest)
    monkeypatch.setattr(
        reconcile, "resolve_app_hook_cwd", lambda *_a, **_kw: (tmp_path, None)
    )
    monkeypatch.setattr(
        reconcile,
        "execute_app_hook_slot",
        lambda *_a, **_kw: HookRunResult(
            service="truealpha/app",
            slot="scheduled_audit",
            environment="staging",
            app_url="https://truealpha-staging.truealpha.club",
            version_ref="v0.2.87",
            status="fail",
            exit_code=1,
            duration_seconds=0.1,
            summary="nightly verdict failed",
        ),
    )
    monkeypatch.setattr(reconcile, "record_app_hook_issue_trail", lambda *_a, **_kw: 0)
    sched_out = tmp_path / "scheduled-audit-result.json"
    rc = main(
        [
            "--repo-root",
            str(tmp_path),
            "--run-scheduled-audit",
            "--sync-service",
            "truealpha/app",
            "--output-json",
            str(sched_out),
        ]
    )
    capsys.readouterr()
    assert rc == 1
    saved = json.loads(sched_out.read_text(encoding="utf-8"))
    assert set(saved) == {"scheduled_audit_results"}
    assert saved["scheduled_audit_results"][0]["summary"] == "nightly verdict failed"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _origin_with_release_tag(tmp_path: Path) -> tuple[Path, str, str, str]:
    """A real git origin. Commit 1 adds the text `"needle"` to f.txt. Commit 2 changes
    another line and carries the annotated tag v1.2.3. Commit 3 is the head of main.
    Returns (origin, commit1, commit2, commit3)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "uploadpack.allowFilter", "true")
    (origin / "f.txt").write_text('"needle"\nA\n')
    _git(origin, "add", "f.txt")
    _git(origin, "commit", "-q", "-m", "one")
    first = _git(origin, "rev-parse", "HEAD")
    (origin / "f.txt").write_text('"needle"\nB\n')
    _git(origin, "commit", "-q", "-am", "two")
    tagged = _git(origin, "rev-parse", "HEAD")
    _git(origin, "tag", "-a", "v1.2.3", "-m", "release")
    (origin / "f.txt").write_text('"needle"\nC\n')
    _git(origin, "commit", "-q", "-am", "three")
    return origin, first, tagged, _git(origin, "rev-parse", "HEAD")


def _checkout_of(monkeypatch, origin: Path, sha: str, **kwargs):
    monkeypatch.setitem(reconcile.APP_SOURCES, "test/app", "test/app")
    monkeypatch.setattr(reconcile, "git_url", lambda _slug: origin.as_uri())
    monkeypatch.setattr(reconcile, "SERVICE_TO_SUBMODULE_PATH", {})
    return reconcile.resolve_app_hook_cwd("test/app", sha, **kwargs)


def _recording_runner(argvs: list[list[str]]):
    def run(argv, **kwargs):
        argvs.append(list(argv))
        return subprocess.run(argv, **kwargs)

    return run


@pytest.mark.parametrize("checkout_at", ["tagged_commit", "later_commit"])
def test_hook_checkout_dates_a_name_from_the_lineage_of_the_release(
    tmp_path, monkeypatch, checkout_at
) -> None:
    """#1567: the truealpha nightly check runs `git rev-parse <tag>^{commit}` and then
    `git log --reverse -S'"name"' <tag>` in the hook checkout. A depth-1 checkout has no tag
    name, so the first command failed. A depth-1 tag fetch would pass it and answer the
    second command with the release commit (commit 2) in place of commit 1, which is the
    commit that first held the name."""
    origin, first, tagged, head = _origin_with_release_tag(tmp_path)
    # The audit asks for the tag's own commit (`resolve_image_ref` peels the tag), so
    # "tagged_commit" is the real flow. A plain tag fetch sends no history there.
    sha = tagged if checkout_at == "tagged_commit" else head
    cwd, tmp = _checkout_of(monkeypatch, origin, sha, release_ref="v1.2.3")
    try:
        # Checked first: `git log -S` below downloads the old file contents it needs.
        missing = _git(cwd, "rev-list", "--objects", "--missing=print", "v1.2.3")
        assert any(line.startswith("?") for line in missing.splitlines())
        assert _git(cwd, "rev-parse", "--verify", "v1.2.3^{commit}") == tagged
        dated = _git(
            cwd,
            "log",
            "--reverse",
            "--format=%H",
            "-S",
            '"needle"',
            "v1.2.3",
            "--",
            "f.txt",
        ).splitlines()
        assert dated[0] == first
        assert _git(cwd, "rev-list", "--count", "v1.2.3") == "2"
        # The checked-out commit is the one that was asked for, not the tag.
        assert _git(cwd, "rev-parse", "HEAD") == sha
        assert (cwd / "f.txt").read_text() == (
            '"needle"\nB\n' if checkout_at == "tagged_commit" else '"needle"\nC\n'
        )
    finally:
        assert tmp is not None
        tmp.cleanup()


def test_hook_checkout_without_a_release_ref_has_no_tag_names(
    tmp_path, monkeypatch
) -> None:
    """Control for the test above: with no release ref the checkout is the old depth-1 one."""
    origin, _first, tagged, _head = _origin_with_release_tag(tmp_path)
    cwd, tmp = _checkout_of(monkeypatch, origin, tagged)
    try:
        assert _git(cwd, "tag", "--list") == ""
        with pytest.raises(subprocess.CalledProcessError):
            _git(cwd, "rev-parse", "--verify", "v1.2.3^{commit}")
        assert _git(cwd, "rev-list", "--count", "HEAD") == "1"
    finally:
        assert tmp is not None
        tmp.cleanup()


@pytest.mark.parametrize(
    "ref", ["main", "", "v1.2", "--upload-pack=x", "v1.2.3\n", "v1.2.3:refs/heads/x"]
)
def test_hook_checkout_fetches_only_a_vXYZ_release_tag(
    tmp_path, monkeypatch, ref
) -> None:
    """The fetch guard is `RELEASE_TAG_RE`. The runner records every command, so a ref that
    the guard should stop shows up as a `fetch ... tag` call even when git itself would
    refuse it."""
    origin, _first, tagged, _head = _origin_with_release_tag(tmp_path)
    argvs: list[list[str]] = []
    cwd, tmp = _checkout_of(
        monkeypatch, origin, tagged, release_ref=ref, runner=_recording_runner(argvs)
    )
    try:
        assert cwd != reconcile.REPO_ROOT
        assert [a for a in argvs if "tag" in a] == []
    finally:
        assert tmp is not None
        tmp.cleanup()


def test_hook_checkout_runs_one_blobless_full_history_tag_fetch(
    tmp_path, monkeypatch
) -> None:
    origin, _first, tagged, _head = _origin_with_release_tag(tmp_path)
    argvs: list[list[str]] = []
    _cwd, tmp = _checkout_of(
        monkeypatch,
        origin,
        tagged,
        release_ref="v1.2.3",
        runner=_recording_runner(argvs),
    )
    try:
        tag_fetches = [a for a in argvs if a[:2] == ["git", "fetch"] and "tag" in a]
        assert len(tag_fetches) == 1
        assert tag_fetches[0][-2:] == ["tag", "v1.2.3"]
        assert "--filter=blob:none" in tag_fetches[0]
        assert "--no-tags" in tag_fetches[0]
        assert "--depth=2147483647" in tag_fetches[0]
        assert "--depth" not in tag_fetches[0]
        assert "--unshallow" not in tag_fetches[0]
    finally:
        assert tmp is not None
        tmp.cleanup()


def test_hook_checkout_survives_a_refused_tag_fetch_and_says_so(
    tmp_path, monkeypatch, capsys
) -> None:
    """A tag that the origin lacks leaves a valid checkout and one ::warning:: line."""
    origin, _first, tagged, _head = _origin_with_release_tag(tmp_path)
    cwd, tmp = _checkout_of(monkeypatch, origin, tagged, release_ref="v9.9.9")
    try:
        assert cwd != reconcile.REPO_ROOT
        assert _git(cwd, "rev-parse", "HEAD") == tagged
        err = capsys.readouterr().err
        assert "::warning::release tag v9.9.9 was not fetched for test/app" in err
    finally:
        assert tmp is not None
        tmp.cleanup()


@pytest.mark.parametrize(
    ("slot", "expected"),
    [
        ("scheduled_audit", "v1.2.3"),
        ("smoke", ""),
        ("staging_gate", ""),
        ("prod_gate", ""),
        ("preview_gate", ""),
    ],
)
def test_release_ref_is_needed_only_by_the_slot_that_declares_checkout_lineage(
    slot, expected
) -> None:
    from infra2_sdk.manifests import HOOK_SLOTS, RuntimeHooksManifest

    manifest = RuntimeHooksManifest.from_dict(
        {
            "schema_version": 2,
            "service": "truealpha/app",
            "slots": {
                s: {
                    "run": "echo ok",
                    "timeout_seconds": 30,
                    "checkout_lineage": s == "scheduled_audit",
                }
                for s in HOOK_SLOTS
            },
        }
    )
    assert reconcile._slot_release_ref(manifest, (slot,), "v1.2.3") == expected


def _record_hook_checkouts(monkeypatch, tmp_path) -> list[tuple[str, str]]:
    """Stub the hook environment of a reconcile run; return the (service, release_ref)
    of every hook checkout that the run asks for."""
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
            "schema_version": 2,
            "service": "finance_report/app",
            "slots": {
                slot_name: {
                    "run": "echo ok",
                    "bootstrap": "",
                    "timeout_seconds": 30,
                    "required_env": [],
                    "optional_env": [],
                    "artifacts_dir": f"artifacts/{slot_name}",
                    "checkout_lineage": slot_name == "scheduled_audit",
                }
                for slot_name in HOOK_SLOTS
            },
        }
    )
    monkeypatch.setattr(reconcile, "fetch_hooks_manifest", lambda *_a, **_kw: manifest)
    monkeypatch.setattr(
        reconcile,
        "execute_app_hook_slot",
        lambda _m, slot, *, app_url, environment, version_ref, **_kw: HookRunResult(
            service="finance_report/app",
            slot=slot,
            environment=environment,
            app_url=app_url,
            version_ref=version_ref,
            status="pass",
            exit_code=0,
            duration_seconds=0.1,
            summary="ok",
        ),
    )
    monkeypatch.setattr(reconcile, "record_app_hook_issue_trail", lambda *_a, **_kw: 0)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    seen: list[tuple[str, str]] = []

    def _fake_checkout(service, _sha, *, release_ref="", **_kw):
        seen.append((service, release_ref))
        return tmp_path, None

    monkeypatch.setattr(reconcile, "resolve_app_hook_cwd", _fake_checkout)
    return seen


def test_scheduled_audit_asks_for_the_release_tag_of_each_app(
    monkeypatch, tmp_path
) -> None:
    """The call site of #1567: the scheduled audit passes the pinned release of each App."""
    seen = _record_hook_checkouts(monkeypatch, tmp_path)
    reconcile.run_scheduled_app_audits(tmp_path, runner=subprocess.run)
    assert dict(seen) == {"finance_report/app": "v0.2.50", "truealpha/app": "v0.2.83"}


def test_post_deploy_smoke_asks_for_no_release_tag(monkeypatch, tmp_path) -> None:
    """A production smoke run must not gain a fetch, whereas a lineage slot in post-deploy does."""
    seen = _record_hook_checkouts(monkeypatch, tmp_path)
    plan = build_plan(
        ["finance_report/finance_report/10.app/compose.yaml"],
        app_release_tags={"finance_report/app": "v0.2.50"},
    )
    applied = [
        reconcile.DeployCommand(
            service="finance_report/app", deploy_type="prod", argv=["true"]
        )
    ]
    reconcile.run_post_deploy_app_hooks(
        plan,
        applied,
        repo_root=tmp_path,
        after="v1.2.3",
        target_env="production",
        slot="smoke",
        record_issues=False,
    )
    assert seen == [("finance_report/app", "")]

    seen.clear()
    reconcile.run_post_deploy_app_hooks(
        plan,
        applied,
        repo_root=tmp_path,
        after="v1.2.3",
        target_env="staging",
        slot="smoke,scheduled_audit",
        record_issues=False,
    )
    assert seen == [("finance_report/app", "v0.2.50")]


@pytest.mark.parametrize(
    ("slot", "expected"),
    [
        ("scheduled_audit,smoke", "v0.2.50"),
        ("smoke,scheduled_audit", "v0.2.50"),
        (" smoke , scheduled_audit ", "v0.2.50"),
        ("smoke,staging_gate", ""),
    ],
)
def test_post_deploy_asks_for_the_release_tag_whichever_slot_reads_the_lineage(
    monkeypatch, tmp_path, slot, expected
) -> None:
    """The slot list of one post-deploy run: the tag is wanted when ANY slot reads the lineage,
    wherever it stands in the list (a "last slot only" or "first slot only" rule fails)."""
    seen = _record_hook_checkouts(monkeypatch, tmp_path)
    plan = build_plan(
        ["finance_report/finance_report/10.app/compose.yaml"],
        app_release_tags={"finance_report/app": "v0.2.50"},
    )
    applied = [
        reconcile.DeployCommand(
            service="finance_report/app", deploy_type="staging", argv=["true"]
        )
    ]
    reconcile.run_post_deploy_app_hooks(
        plan,
        applied,
        repo_root=tmp_path,
        after="v1.2.3",
        target_env="staging",
        slot=slot,
        record_issues=False,
    )
    assert seen == [("finance_report/app", expected)]


@pytest.mark.parametrize("with_source_sha", [True, False])
@pytest.mark.parametrize(
    ("slot", "expected"), [("scheduled_audit", "v0.2.50"), ("smoke", "")]
)
def test_run_hook_slot_passes_the_release_tag_only_for_the_audit_slot(
    monkeypatch, tmp_path, slot, expected, with_source_sha
) -> None:
    """Both service branches of --run-hook-slot: with an explicit source SHA, and with the
    SHA resolved from the pinned release."""
    seen = _record_hook_checkouts(monkeypatch, tmp_path)
    argv = [
        "--repo-root",
        str(tmp_path),
        "--run-hook-slot",
        slot,
        "--hook-service",
        "finance_report/app",
        "--version-ref",
        "v0.2.50",
        "--target-url",
        "https://report-staging.example",
    ]
    if with_source_sha:
        argv += ["--hook-source-sha", SHA]
    rc = main(argv)
    assert rc == 0
    assert seen == [("finance_report/app", expected)]


def test_scheduled_audit_function_asks_for_no_tag_for_another_slot(
    monkeypatch, tmp_path
) -> None:
    """The slot gate at the audit call site: `run_scheduled_app_audits` with a slot that does
    not read the lineage must not ask for the release tag."""
    seen = _record_hook_checkouts(monkeypatch, tmp_path)
    reconcile.run_scheduled_app_audits(tmp_path, slot="smoke", runner=subprocess.run)
    assert seen
    assert {ref for _svc, ref in seen} == {""}


def test_pre_apply_guard_runs_train_version_match_before_any_unit_deploys(
    monkeypatch, tmp_path
) -> None:
    """#1544: every pre-apply App guard that deploy_v2 runs (including
    assert_app_version_ref_matches_train) MUST run in reconcile.main() BEFORE
    run_deploy_commands touches the first unit. When unit N (truealpha/app) violates
    harness/repos.yaml, zero earlier platform or App units may be deployed."""
    repos_yaml = tmp_path / "harness" / "repos.yaml"
    repos_yaml.parent.mkdir(parents=True, exist_ok=True)
    repos_yaml.write_text(
        "schema_version: 1\n"
        "repositories:\n"
        "  - id: finance-report\n"
        "    role: external-application\n"
        "    release_tag: v0.2.61\n"
        "  - id: truealpha\n"
        "    role: external-application\n"
        "    release_tag: v0.2.89\n",
        encoding="utf-8",
    )

    deploy_calls: list[list[str]] = []
    monkeypatch.setattr(reconcile, "assert_after_on_main", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        reconcile, "assert_production_marker_advances", lambda *_a, **_kw: "v3.1.49"
    )
    monkeypatch.setattr(
        reconcile, "assert_app_version_ref_on_main", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(
        reconcile,
        "run_deploy_commands",
        lambda cmds, **_kw: deploy_calls.extend([list(c.argv) for c in cmds]) or [],
    )

    with pytest.raises(
        SystemExit,
        match="version_ref 'v0.2.88' for truealpha/app \\(prod\\) does not match harness/repos.yaml release_tag 'v0.2.89'",
    ):
        main(
            [
                "--repo-root",
                str(tmp_path),
                "--before",
                "v3.1.49",
                "--after",
                "v3.1.50",
                "--promote-prod",
                "--changed-file",
                "platform/12.alerting/compose.yaml",
                "--changed-file",
                "harness/repos.yaml",
                "--app-release-tag",
                "truealpha/app=v0.2.88",
            ]
        )
    assert deploy_calls == []


def test_run_post_deploy_app_hooks_executes_services_concurrently_and_stops_slot_chain_per_service(
    monkeypatch, tmp_path
) -> None:
    """#1608, #1602 (S3.1): run_post_deploy_app_hooks executes deployed services concurrently
    so total wall-clock is bounded by max(services) rather than sum(services), while stopping
    a failing service's own smoke->staging_gate chain without dropping sibling verdicts."""
    import threading
    import time as _time
    from infra2_sdk.manifests import HOOK_SLOTS, HookRunResult, RuntimeHooksManifest
    from infra2_sdk.refs import ResolvedRef

    repos_yaml = tmp_path / "harness" / "repos.yaml"
    repos_yaml.parent.mkdir(parents=True, exist_ok=True)
    repos_yaml.write_text(
        "schema_version: 1\n"
        "repositories:\n"
        "  - id: finance-report\n"
        "    role: external-application\n"
        "    release_tag: v0.2.61\n"
        "  - id: truealpha\n"
        "    role: external-application\n"
        "    release_tag: v0.2.89\n",
        encoding="utf-8",
    )
    todo_dir = tmp_path / "platform" / "30.todo"
    todo_dir.mkdir(parents=True, exist_ok=True)
    (todo_dir / "runtime-hooks.manifest.json").write_text(
        (ROOT / "platform" / "30.todo" / "runtime-hooks.manifest.json").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        reconcile,
        "resolve_image_ref",
        lambda ref, **_kw: ResolvedRef(sha=SHA, image_ref=ref, form="tag"),
    )
    monkeypatch.setattr(
        reconcile, "resolve_app_hook_cwd", lambda *_a, **_kw: (tmp_path, None)
    )

    def _fake_fetch(service: str, _sha: str, **_kw):
        return RuntimeHooksManifest.from_dict(
            {
                "schema_version": 2,
                "service": service,
                "slots": {
                    s: {"run": "echo ok", "timeout_seconds": 30} for s in HOOK_SLOTS
                },
            }
        )

    monkeypatch.setattr(reconcile, "fetch_hooks_manifest", _fake_fetch)

    barrier = threading.Barrier(3, timeout=2.0)
    lock = threading.Lock()
    seen: list[tuple[str, str]] = []

    def _concurrent_exec(
        manifest, slot: str, *, app_url: str, environment: str, version_ref: str, **_kw
    ):
        if slot == "smoke":
            barrier.wait()  # proves all 3 services run concurrently in separate threads!
        _time.sleep(0.05)
        with lock:
            seen.append((manifest.service, slot))
        failed = manifest.service == "finance_report/app" and slot == "smoke"
        return HookRunResult(
            service=manifest.service,
            slot=slot,
            environment=environment,
            app_url=app_url,
            version_ref=version_ref,
            status="fail" if failed else "pass",
            exit_code=1 if failed else 0,
            duration_seconds=0.05,
            summary="smoke failed" if failed else "ok",
        )

    monkeypatch.setattr(reconcile, "execute_app_hook_slot", _concurrent_exec)

    plan = build_plan(
        ["platform/30.todo/app.py", "harness/repos.yaml"],
        repo_root=tmp_path,
        app_release_tags={
            "finance_report/app": "v0.2.61",
            "truealpha/app": "v0.2.89",
        },
    )
    applied = [
        reconcile.DeployCommand("platform/todo", "staging", ["true"]),
        reconcile.DeployCommand("finance_report/app", "staging", ["true"]),
        reconcile.DeployCommand("truealpha/app", "staging", ["true"]),
    ]
    results = reconcile.run_post_deploy_app_hooks(
        plan,
        applied,
        repo_root=tmp_path,
        after="v3.1.50",
        target_env="staging",
        slot="smoke,staging_gate",
        record_issues=False,
    )
    # finance_report/app stopped after failed smoke, while platform/todo & truealpha/app
    # still ran both smoke and staging_gate concurrently and reported full verdicts!
    by_svc_slot = {(r["service"], r["slot"]): r["ok"] for r in results}
    assert by_svc_slot == {
        ("finance_report/app", "smoke"): False,
        ("platform/todo", "smoke"): True,
        ("platform/todo", "staging_gate"): True,
        ("truealpha/app", "smoke"): True,
        ("truealpha/app", "staging_gate"): True,
    }


def test_print_changed_app_releases_cli_for_cumulative_promotion_guard(
    capsys, tmp_path
) -> None:
    """#1563: --print-changed-app-releases --before <marker> --after <tag> emits
    SERVICE=TAG lines for every App service selected across cumulative tags."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "harness").mkdir()
    (repo / "docs" / "ssot").mkdir(parents=True)
    (repo / "docs" / "ssot" / "deploy-dependencies.yaml").write_text(
        (ROOT / "docs" / "ssot" / "deploy-dependencies.yaml").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    (repo / "harness" / "repos.yaml").write_text(
        "schema_version: 1\n"
        "repositories:\n"
        "  - id: finance-report\n"
        "    role: external-application\n"
        "    release_tag: v0.2.60\n"
        "  - id: truealpha\n"
        "    role: external-application\n"
        "    release_tag: v0.2.87\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "v3.1.48")
    _git(repo, "tag", "v3.1.48")

    # v3.1.49 bumps finance-report
    (repo / "harness" / "repos.yaml").write_text(
        "schema_version: 1\n"
        "repositories:\n"
        "  - id: finance-report\n"
        "    role: external-application\n"
        "    release_tag: v0.2.61\n"
        "  - id: truealpha\n"
        "    role: external-application\n"
        "    release_tag: v0.2.87\n",
        encoding="utf-8",
    )
    _git(repo, "commit", "-q", "-am", "v3.1.49")
    _git(repo, "tag", "v3.1.49")

    # v3.1.50 bumps truealpha
    (repo / "harness" / "repos.yaml").write_text(
        "schema_version: 1\n"
        "repositories:\n"
        "  - id: finance-report\n"
        "    role: external-application\n"
        "    release_tag: v0.2.61\n"
        "  - id: truealpha\n"
        "    role: external-application\n"
        "    release_tag: v0.2.89\n",
        encoding="utf-8",
    )
    _git(repo, "commit", "-q", "-am", "v3.1.50")
    _git(repo, "tag", "v3.1.50")

    rc = main(
        [
            "--repo-root",
            str(repo),
            "--print-changed-app-releases",
            "--before",
            "v3.1.48",
            "--after",
            "v3.1.50",
        ]
    )
    assert rc == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines == [
        "finance_report/app=v0.2.61",
        "truealpha/app=v0.2.89",
    ]


def test_summary_is_rendered_from_records(monkeypatch, tmp_path) -> None:
    """The run summary holds the records of the run and nothing that the plan builds (#1645)."""
    from common.deploy.stage_record import write_stage_record
    from infra2_sdk.observability import CheckVerdict

    def record(phase, checks, **kw):
        return write_stage_record(
            env="staging.release_validation",
            phase=phase,
            subject="tag:v3.1.23",
            ref="refs/tags/v3.1.23",
            event="tag",
            run_url="https://github.com/o/r/actions/runs/777",
            started_at="2026-11-01T00:00:00Z",
            checks=checks,
            units=("infra:platform/alerting", "sdk:3.29.0"),
            persist=False,
            **kw,
        )

    records = [
        record("pre", [CheckVerdict("contract", True, seconds=0.5)]),
        record(
            "operation",
            [
                CheckVerdict(
                    "staging:platform/alerting",
                    False,
                    "attempt 1: a|b\nc",
                    seconds=12.5,
                )
            ],
            uncovered=("infra:platform/alerting",),
        ),
    ]
    target = tmp_path / "summary.md"
    reconcile.write_summary(records=records, dry_run=False, path=str(target))
    text = target.read_text(encoding="utf-8")
    assert "### pre: pass" in text and "### operation: fail" in text
    assert "| `contract` | ok | 0.5 |  |" in text
    assert (
        "| `staging:platform/alerting` | FAILED | 12.5 | attempt 1: a\\|b c |" in text
    )
    assert "- Units: `infra:platform/alerting, sdk:3.29.0`" in text
    assert "- Uncovered by a hook: `infra:platform/alerting`" in text
    for plan_line in (
        "Changed files",
        "Selected services",
        "Dropped files",
        "Commands:",
        "```json",
    ):
        assert plan_line not in text
    # No record, no summary: the plan alone is not a source.
    empty = tmp_path / "empty.md"
    reconcile.write_summary(records=[], dry_run=False, path=str(empty))
    assert not empty.exists()
