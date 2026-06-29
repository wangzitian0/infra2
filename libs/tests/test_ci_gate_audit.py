"""Drift audit for the infra CI gate inventory (#461)."""
from __future__ import annotations

from pathlib import Path

from tools.ci_gate_audit import audit, audit_gates, main

ROOT = Path(__file__).resolve().parents[2]
INFRA_CI = ".github/workflows/infra-ci.yml"


def _gate(**over):
    base = dict(
        id="infra_ci.compose_validate",
        stage="github_ci.merge_authority",
        task_category="compose_validate",
        workflow=INFRA_CI,
        job="validate-compose",
    )
    base.update(over)
    return base


def test_real_inventory_has_clean_schema_and_no_dangling() -> None:
    result = audit()
    assert result["schema_errors"] == []
    assert result["dangling_gates"] == []


def test_infra_ci_is_fully_covered() -> None:
    # every job of infra-ci.yml is registered (no unregistered within the covered workflow)
    result = audit()
    assert [u for u in result["unregistered_jobs"] if "infra-ci.yml" in u] == []


def test_report_mode_exit_zero_when_clean() -> None:
    assert main([]) == 0


def test_dangling_gate_is_hard() -> None:
    r = audit_gates([_gate(job="does-not-exist")], root=ROOT, prefix="infra_ci.", known_ci_workflows=())
    assert r["dangling_gates"]


def test_unknown_stage_is_schema_error() -> None:
    r = audit_gates([_gate(stage="bogus.stage")], root=ROOT, prefix="infra_ci.", known_ci_workflows=())
    assert any("unknown stage" in e for e in r["schema_errors"])


def test_wrong_prefix_is_schema_error() -> None:
    r = audit_gates([_gate(id="ci.compose_validate")], root=ROOT, prefix="infra_ci.", known_ci_workflows=())
    assert any("prefix" in e for e in r["schema_errors"])


def test_unregistered_job_detected() -> None:
    # cover only one job of infra-ci.yml -> the other jobs are unregistered
    r = audit_gates([_gate()], root=ROOT, prefix="infra_ci.", known_ci_workflows=(INFRA_CI,))
    assert any("infra-ci.yml" in u for u in r["unregistered_jobs"])


def test_empty_workflow_does_not_crash() -> None:
    # a gate with empty `workflow` resolves to the repo root dir; the audit must report it
    # (dangling + schema error), not crash with IsADirectoryError.
    r = audit_gates([_gate(workflow="")], root=ROOT, prefix="infra_ci.", known_ci_workflows=())
    assert r["dangling_gates"] and r["schema_errors"]
