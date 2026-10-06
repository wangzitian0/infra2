"""A workflow step that resolves the deployment environment names it (#1039).

``get_env()`` has no default. A workflow step that runs a task or tool reaching it must
carry DEPLOY_ENV or INFRA_ENVIRONMENT, set on the workflow, the job or the step. Without
one, the step stops at once with EnvironmentNotSetError.

This table is the caller map of #1039 for workflows. A new ``invoke`` step fails the
first test until its author classifies it here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"

# Command fragment of a step -> the environment the step must name.
ENVIRONMENT_READING_COMMANDS = {
    "fr-observability.shared.apply-alerts": "production",
    "fr-observability.shared.apply-dashboard": "production",
    "tools/signoz_alert_rule_probe.py": "production",
}

# ``invoke`` tasks that never call get_env().
ENVIRONMENT_FREE_INVOKE_TASKS = {"dokploy.audit-autodeploy"}

_INVOKE_TASK = re.compile(r"\binvoke\s+([A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+)")
_ENVIRONMENT_VARIABLES = ("INFRA_ENVIRONMENT", "DEPLOY_ENV")


def _steps():
    """Yield (workflow name, job name, effective env of the step, run text) per run step."""
    for path in sorted(WORKFLOWS.glob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in (document.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                run = step.get("run")
                if not run:
                    continue
                env = {
                    **(document.get("env") or {}),
                    **(job.get("env") or {}),
                    **(step.get("env") or {}),
                }
                yield path.name, job_name, env, run


def _named_environment(env: dict) -> str | None:
    for variable in _ENVIRONMENT_VARIABLES:
        value = str(env.get(variable) or "").strip()
        if value:
            return value
    return None


def test_every_workflow_invoke_task_is_classified() -> None:
    found = {match for _, _, _, run in _steps() for match in _INVOKE_TASK.findall(run)}
    classified = set(ENVIRONMENT_READING_COMMANDS) | ENVIRONMENT_FREE_INVOKE_TASKS
    assert found - classified == set(), (
        "Classify each new workflow `invoke` task: add it to "
        "ENVIRONMENT_READING_COMMANDS if it reaches get_env(), else to "
        "ENVIRONMENT_FREE_INVOKE_TASKS."
    )


def test_the_classification_tables_match_real_workflow_steps() -> None:
    """A table entry no step runs would leave the contract guarding nothing."""
    runs = [run for _, _, _, run in _steps()]
    for command in (*ENVIRONMENT_READING_COMMANDS, *ENVIRONMENT_FREE_INVOKE_TASKS):
        assert any(command in run for run in runs), command


@pytest.mark.parametrize("command", sorted(ENVIRONMENT_READING_COMMANDS))
def test_a_step_that_reads_the_environment_names_it(command: str) -> None:
    expected = ENVIRONMENT_READING_COMMANDS[command]
    matching = [
        (workflow, job, env) for workflow, job, env, run in _steps() if command in run
    ]
    assert matching, command
    for workflow, job, env in matching:
        assert _named_environment(env) == expected, (
            f"{workflow} job {job!r} runs {command} without "
            f"INFRA_ENVIRONMENT or DEPLOY_ENV set to {expected!r}"
        )
