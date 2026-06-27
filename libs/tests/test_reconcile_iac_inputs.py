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
    is_zero_sha,
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


def test_guard_rejects_unresolvable_ref() -> None:
    def runner(argv, **_kwargs):
        if argv[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(argv, 128, "", "unknown revision")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(SystemExit, match="cannot resolve"):
        assert_after_on_main("v9.9.9", ROOT, runner=runner)


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
