"""Drift audit for the infra CI gate inventory (#461).

Also covers the scope-reporting contract added when the audit's coverage boundary turned
out to be undocumented anywhere a reader could actually see it (only a source comment said
"Phase 1: only infra-ci", while KNOWN_CI_WORKFLOWS never grew past that and the epic that
would have grown it, Infra-016 #459, was closed "not planned"): `audit()`'s JSON output must
itself name which workflows are covered and which are not, and infra-ci.yml's own step must
run with `--enforce` now that it is provably fully covered.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from tools.ci_gate_audit import KNOWN_CI_WORKFLOWS, audit, audit_gates, main

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
    r = audit_gates(
        [_gate(job="does-not-exist")],
        root=ROOT,
        prefix="infra_ci.",
        known_ci_workflows=(),
    )
    assert r["dangling_gates"]


def test_unknown_stage_is_schema_error() -> None:
    r = audit_gates(
        [_gate(stage="bogus.stage")],
        root=ROOT,
        prefix="infra_ci.",
        known_ci_workflows=(),
    )
    assert any("unknown stage" in e for e in r["schema_errors"])


def test_wrong_prefix_is_schema_error() -> None:
    r = audit_gates(
        [_gate(id="ci.compose_validate")],
        root=ROOT,
        prefix="infra_ci.",
        known_ci_workflows=(),
    )
    assert any("prefix" in e for e in r["schema_errors"])


def test_unregistered_job_detected() -> None:
    # cover only one job of infra-ci.yml -> the other jobs are unregistered
    r = audit_gates(
        [_gate()], root=ROOT, prefix="infra_ci.", known_ci_workflows=(INFRA_CI,)
    )
    assert any("infra-ci.yml" in u for u in r["unregistered_jobs"])


def test_empty_workflow_does_not_crash() -> None:
    # a gate with empty `workflow` resolves to the repo root dir; the audit must report it
    # (dangling + schema error), not crash with IsADirectoryError.
    r = audit_gates(
        [_gate(workflow="")], root=ROOT, prefix="infra_ci.", known_ci_workflows=()
    )
    assert r["dangling_gates"] and r["schema_errors"]


# -- Scope-reporting contract -------------------------------------------------------


def test_audit_output_names_its_own_coverage_boundary() -> None:
    """A reader of the printed JSON (not just this source file) must be able to
    see which workflows the audit does and does not check."""
    result = audit()
    assert result["covered_workflows"] == sorted(KNOWN_CI_WORKFLOWS)
    assert result["out_of_scope_workflows"], (
        "sanity: infra2 has more than one workflow, so some must be out of scope"
    )
    assert set(result["covered_workflows"]).isdisjoint(result["out_of_scope_workflows"])

    # Every *.yml under .github/workflows/ is accounted for on one side or the other --
    # a new workflow file must show up as newly out-of-scope, not vanish silently.
    all_workflows = {
        f".github/workflows/{p.name}"
        for p in (ROOT / ".github" / "workflows").glob("*.yml")
    }
    accounted = set(result["covered_workflows"]) | set(result["out_of_scope_workflows"])
    assert all_workflows <= accounted


def test_enforce_mode_fails_closed_on_an_unregistered_job(monkeypatch) -> None:
    """Within the covered scope, --enforce must actually turn drift into a
    non-zero exit -- not just a printed line."""
    import tools.ci_gate_audit as mod

    monkeypatch.setattr(
        mod,
        "audit",
        lambda root=mod.ROOT: {
            "schema_errors": [],
            "dangling_gates": [],
            "unregistered_jobs": [f"{INFRA_CI}:some-new-job"],
            "covered_workflows": [INFRA_CI],
            "out_of_scope_workflows": [],
        },
    )
    assert mod.main(["--enforce"]) == 1
    assert mod.main([]) == 0, "report-only mode must stay non-blocking on the same drift"


def test_infra_ci_step_runs_the_audit_with_enforce() -> None:
    """infra-ci.yml's own ci_gate_audit step must pass --enforce now that
    infra-ci.yml is provably fully covered (see the test above this one)."""
    workflow = yaml.safe_load((ROOT / INFRA_CI).read_text(encoding="utf-8"))
    steps = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "tools.ci_gate_audit" in str(step.get("run") or "")
    ]
    assert len(steps) == 1, f"expected exactly one ci_gate_audit step, got {steps}"
    assert "--enforce" in steps[0]["run"]
