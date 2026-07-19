"""Tests for main-push input-drift reconcile of iac_pinned services."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from tools.reconcile_iac_inputs import (
    MANIFEST_PATH,
    assert_after_on_main,
    build_deploy_commands,
    build_plan,
    commands_to_apply,
    is_zero_sha,
    main,
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
    plan = build_plan(["finance_report/finance_report/10.app/compose.yaml"])

    assert "finance_report/app" in plan.ignored
    assert plan.services == []


def test_manifest_change_reconciles_all_iac_pinned_services() -> None:
    plan = build_plan([MANIFEST_PATH])

    assert "platform/alerting" in plan.services
    assert "finance_report/postgres" in plan.services
    assert "finance_report/app" not in plan.services
    assert "dependency manifest changed" in plan.selected["platform/alerting"]


def test_prod_only_services_skip_staging() -> None:
    plan = build_plan(["platform/11.signoz/compose.yaml"])

    assert "platform/signoz" not in plan.staging_services
    assert plan.prod_services == ["platform/signoz"]


def test_not_yet_in_production_services_skip_prod() -> None:
    """#542 (the v1.1.34 2/14 prod-promote failure): an iac_pinned service whose
    Deployer declares `not_yet_in_production = True` (truealpha's staging-scoped
    rollout, #500 — no prod composes or Vault provisioning) must fan out to
    staging only, and be surfaced in the plan rather than silently dropped."""
    plan = build_plan([MANIFEST_PATH])  # selects every iac_pinned service

    # data_engine still carries the flag; postgres went LIVE in prod 2026-07-19
    # (verified: truealpha-postgres running) and its flag was removed — the plan
    # must include it for prod again, automatically.
    for service in ("truealpha/data_engine",):
        assert service in plan.selected
        assert service in plan.staging_services
        assert service not in plan.prod_services
        assert service in plan.not_yet_in_production
    assert "truealpha/postgres" in plan.prod_services
    assert "truealpha/postgres" not in plan.not_yet_in_production
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
    return build_deploy_commands(
        plan, iac_ref=SHA, domain="zitian.party", timeout=600
    )


def test_default_applies_staging_only() -> None:
    # release decoupling: a tag push auto-applies staging (soak), never prod.
    applied = commands_to_apply(
        _alerting_commands(), dry_run=False, promote_prod=False
    )
    assert [c.deploy_type for c in applied] == ["staging"]


def test_promote_prod_applies_prod_only() -> None:
    # prod is a separate, explicit promotion step.
    applied = commands_to_apply(
        _alerting_commands(), dry_run=False, promote_prod=True
    )
    assert [c.deploy_type for c in applied] == ["prod"]


def test_dry_run_applies_nothing() -> None:
    commands = _alerting_commands()
    assert commands_to_apply(commands, dry_run=True, promote_prod=False) == []
    assert commands_to_apply(commands, dry_run=True, promote_prod=True) == []


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
    assert "promote_prod" in workflow["on"]["workflow_dispatch"]["inputs"]
    assert "--promote-prod" in text
    # the provenance guard relies on origin/main being resolvable in a tag-push checkout;
    # lock the exact fetch step so a future workflow edit can't silently break the guard.
    assert "refs/remotes/origin/main" in text
