"""Contract: every workflow step holds exactly one of `uses` and `run` (#1130).

GitHub rejects a workflow file that has a step with both keys. The run then ends with
`failure` and zero jobs, and nothing in the repository reports why. #1116 left such a step
in `deploy.yml`. The deploy workflow did not start until the step was repaired.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = ROOT / ".github" / "workflows"


def _load_workflows(directory: Path) -> dict[str, dict]:
    return {
        path.name: yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for path in sorted(directory.glob("*.y*ml"))
    }


def step_shape_errors(workflows: dict[str, dict]) -> list[str]:
    errors: list[str] = []
    for workflow_name, workflow in workflows.items():
        for job_name, job in (workflow.get("jobs") or {}).items():
            for index, step in enumerate(job.get("steps") or [], start=1):
                label = f"{workflow_name}:{job_name} step {index} ({step.get('name', 'unnamed')})"
                has_uses = "uses" in step
                has_run = "run" in step
                if has_uses and has_run:
                    errors.append(f"{label} holds both `uses` and `run`")
                elif not has_uses and not has_run:
                    errors.append(f"{label} holds neither `uses` nor `run`")
    return errors


def test_every_workflow_step_holds_exactly_one_of_uses_and_run():
    workflows = _load_workflows(WORKFLOWS_DIR)
    assert workflows, "no workflow was read"
    assert step_shape_errors(workflows) == []


def test_the_contract_reads_steps_in_the_tree():
    workflows = _load_workflows(WORKFLOWS_DIR)
    steps = sum(
        len(job.get("steps") or [])
        for workflow in workflows.values()
        for job in (workflow.get("jobs") or {}).values()
    )
    assert steps > 50, "the contract read too few steps; the loader is stale"


def _workflow(step: dict) -> dict[str, dict]:
    return {"w.yml": {"jobs": {"j": {"steps": [step]}}}}


def test_a_step_with_uses_and_run_is_reported():
    errors = step_shape_errors(
        _workflow({"name": "x", "uses": "a/b@v1", "run": "echo"})
    )
    assert errors == ["w.yml:j step 1 (x) holds both `uses` and `run`"]


def test_a_step_with_neither_uses_nor_run_is_reported():
    errors = step_shape_errors(_workflow({"name": "x", "with": {"k": "v"}}))
    assert errors == ["w.yml:j step 1 (x) holds neither `uses` nor `run`"]


def test_a_step_with_one_key_passes():
    assert step_shape_errors(_workflow({"uses": "a/b@v1"})) == []
    assert step_shape_errors(_workflow({"run": "echo"})) == []
