"""WS4 (#1647): PR zero-state preview runtime gate contract tests."""

from __future__ import annotations

from pathlib import Path

import yaml
from infra2_sdk.ci import load_delivery_stages, validate_inventory

from common.deploy.env_config import preview_service_config
from common.gate.production_contract import UNGATED_JOBS
from tools import pr_merge_gate as gate
from tools.reconcile_iac_inputs import build_plan

ROOT = Path(__file__).resolve().parents[2]
INFRA_CI_PATH = ROOT / ".github" / "workflows" / "infra-ci.yml"
INVENTORY_PATH = ROOT / "docs" / "ssot" / "ci-gate-inventory.yaml"
NOW = 1_800_000_000.0


def _load_infra_ci() -> dict:
    return yaml.safe_load(INFRA_CI_PATH.read_text(encoding="utf-8"))


def _load_inventory() -> dict:
    return yaml.safe_load(INVENTORY_PATH.read_text(encoding="utf-8"))


def _pr_facts(**overrides) -> gate.HeadFacts:
    required = sorted(gate._required_checks()[0])
    base = dict(
        number=1647,
        state="OPEN",
        draft=False,
        base="main",
        head_sha="abcdef0123456789abcdef0123456789abcdef01",
        files=("platform/30.todo/preview.compose.yaml",),
        last_push_at=NOW - 13 * 60,
        checks=tuple((name, "pass") for name in required),
        unresolved_threads=0,
    )
    base.update(overrides)
    return gate.HeadFacts(**base)


def test_gate_reconcile_plan_builds_step_is_removed() -> None:
    workflow = _load_infra_ci()
    for job_name, job in (workflow.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            step_name = str(step.get("name") or "")
            assert "Gate reconcile plan builds" not in step_name, (
                f"{job_name} still carries {step_name!r}"
            )
    deployer_logic_steps = workflow["jobs"]["test-deployer-logic"]["steps"]
    assert not any(
        "tools.reconcile_iac_inputs" in str(step.get("run") or "")
        for step in deployer_logic_steps
    )


def test_preview_runtime_gate_is_registered_and_blocks_merge() -> None:
    inventory = _load_inventory()
    gates = inventory["gates"]
    validation = validate_inventory(
        gates,
        stage_ids=set(load_delivery_stages()),
        id_prefix=inventory["repo_prefix"],
    )
    assert validation["errors"] == []

    preview_gate = next(
        (entry for entry in gates if entry["id"] == "infra_ci.preview_runtime"),
        None,
    )
    assert preview_gate is not None
    assert preview_gate["stage"] == "preview.runtime"
    assert preview_gate["workflow"] == ".github/workflows/infra-ci.yml"
    assert preview_gate["job"] == "preview"
    assert preview_gate["blocks_merge"] is True
    assert preview_gate["task_category"]


def test_preview_job_wires_unified_deploy_v2_and_always_teardown() -> None:
    workflow = _load_infra_ci()
    preview_job = workflow["jobs"]["preview"]
    assert preview_job["name"] == "Preview Runtime Gate"
    assert preview_job["needs"] == "detect-changes"
    assert (
        preview_job["if"]
        == "github.event_name == 'pull_request' && needs.detect-changes.outputs.has_non_doc == 'true'"
    )
    assert preview_job["permissions"] == {"contents": "read"}
    assert ("infra-ci.yml", "preview") in UNGATED_JOBS

    steps = preview_job["steps"]
    plan_step = next(
        s
        for s in steps
        if s.get("name") == "Plan affected previewable platform services"
    )
    assert "build_plan" in plan_step["run"]
    assert "throwaway_secrets" in plan_step["run"]

    deploy_step = next(
        s
        for s in steps
        if s.get("name") == "Deploy affected previewable platform services"
    )
    deploy_run = deploy_step["run"]
    assert "tools.deploy_v2" in deploy_run
    assert "--type preview/pr" in deploy_run
    assert "${{ github.event.pull_request.number }}" in deploy_run
    assert "${{ github.event.pull_request.head.ref }}" in deploy_run

    down_step = next(s for s in steps if s.get("name") == "Tear down preview slot")
    assert "always()" in str(down_step.get("if") or "")
    down_run = down_step["run"]
    assert "tools.deploy_v2" in down_run
    assert "--type preview/pr" in down_run
    assert "--down" in down_run


def test_build_plan_selects_throwaway_preview_service_for_todo_and_ignores_unrelated() -> (
    None
):
    todo_plan = build_plan(
        ["platform/30.todo/preview.compose.yaml"], app_release_tags={}
    )
    assert "platform/todo" in todo_plan.selected
    assert preview_service_config("platform/todo").throwaway_secrets

    s3_df_plan = build_plan(["platform/03.s3/Dockerfile"], app_release_tags={})
    assert "platform/todo" in s3_df_plan.selected

    unrelated_plan = build_plan(["common/core/environ.py"], app_release_tags={})
    assert "platform/todo" not in unrelated_plan.selected


def test_pr_merge_gate_blocks_when_preview_runtime_gate_fails_or_skips_on_non_doc_pr() -> (
    None
):
    gate._required_checks.cache_clear()
    required = sorted(gate._required_checks()[0])
    assert "Preview Runtime Gate" in required

    green = gate.evaluate(_pr_facts(), now=NOW)
    assert green.ready and green.exit_code == 0

    failed_checks = tuple(
        (name, "fail" if name == "Preview Runtime Gate" else "pass")
        for name in required
    )
    failed = gate.evaluate(_pr_facts(checks=failed_checks), now=NOW)
    assert not failed.ready
    assert failed.exit_code in (2, 3)
    assert any("Preview Runtime Gate" in r for r in failed.reasons)

    skipped_checks = tuple(
        (name, "SKIPPED" if name == "Preview Runtime Gate" else "pass")
        for name in required
    )
    skipped_non_doc = gate.evaluate(_pr_facts(checks=skipped_checks), now=NOW)
    assert not skipped_non_doc.ready
    assert skipped_non_doc.exit_code in (2, 3)
    assert any(
        "Preview Runtime Gate" in r and "skipped" in r for r in skipped_non_doc.reasons
    )

    skipped_doc_only = gate.evaluate(
        _pr_facts(files=("docs/ssot/ops.pipeline.md",), checks=skipped_checks),
        now=NOW,
    )
    assert skipped_doc_only.ready and skipped_doc_only.exit_code == 0
