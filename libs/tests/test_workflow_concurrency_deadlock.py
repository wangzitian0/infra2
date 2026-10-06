"""A job must not share a concurrency group with its own workflow run (#1086).

GitHub cannot start a job whose ``concurrency.group`` equals the group its
workflow run already holds. The job ends ``failure`` with no runner and no step,
so an alert step inside it never runs. #993 renamed the deploy_v2 canary job's
group to ``deploy-v2-canary``, the name the ``37 6 * * *`` schedule gives its
whole run, and the daily scheduled canary stopped starting without a page.

Sharing one job-level group across workflows is intended (it serializes every
canary on the reserved slot). Only a match within one workflow deadlocks.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))

# An expression result is a quoted literal that ends a `&&` branch, or the fixed
# prefix of a format() call.
_LITERAL_RESULT = re.compile(r"&&\s*'([^']+)'\s*(?:\|\||}})")
_FORMAT = re.compile(r"format\(\s*'([^'{]*)\{0\}([^']*)'\s*,\s*([\w.]+)\s*\)")
_INPUT = re.compile(r"^(?:github\.event\.)?inputs\.(\w+)$")


def _group(block) -> str | None:
    if isinstance(block, dict):
        group = block.get("group")
        return str(group) if group is not None else None
    if isinstance(block, str):
        return block
    return None


def _choices(workflow: dict, name: str) -> list[str] | None:
    on = workflow.get(True) or workflow.get("on") or {}
    dispatch = on.get("workflow_dispatch") if isinstance(on, dict) else None
    spec = ((dispatch or {}).get("inputs") or {}).get(name) or {}
    if spec.get("type") == "choice" and spec.get("options"):
        return [str(o) for o in spec["options"]]
    return None


def workflow_group_names(
    group: str, workflow: dict | None = None
) -> tuple[set[str], set[str]]:
    """(exact names, name prefixes) that a workflow-level group can evaluate to.

    A format() over a choice input expands to one name per option; over any other
    value it is an open prefix."""
    if "${{" not in group:
        return {group}, set()
    names = set(_LITERAL_RESULT.findall(group))
    prefixes = set()
    for head, tail, arg in _FORMAT.findall(group):
        match = _INPUT.match(arg)
        options = _choices(workflow or {}, match.group(1)) if match else None
        if options is None:
            prefixes.add(head)
        else:
            names.update(f"{head}{option}{tail}" for option in options)
    return names, prefixes


def deadlocks(workflow: dict) -> list[str]:
    top = _group(workflow.get("concurrency"))
    if not top:
        return []
    names, prefixes = workflow_group_names(top, workflow)
    found = []
    for job_id, job in (workflow.get("jobs") or {}).items():
        group = _group((job or {}).get("concurrency"))
        if group is None or "${{" in group:
            continue
        if group in names or any(group.startswith(p) for p in prefixes if p):
            found.append(f"{job_id}: {group}")
    return found


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_job_shares_a_concurrency_group_with_its_workflow(path: Path) -> None:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert deadlocks(workflow) == []


def test_the_993_shape_is_a_deadlock() -> None:
    """Mutation proof: the workflow as #993 left it must fail the check."""
    workflow = {
        "concurrency": {
            "group": "${{ github.event_name == 'schedule' && "
            "github.event.schedule == '37 6 * * *' && 'deploy-v2-canary' || "
            "format('ops-checks-push-{0}', github.sha) }}"
        },
        "jobs": {"deploy-v2-canary": {"concurrency": {"group": "deploy-v2-canary"}}},
    }
    assert deadlocks(workflow) == ["deploy-v2-canary: deploy-v2-canary"]


def test_a_format_prefix_is_a_deadlock_too() -> None:
    workflow = {
        "concurrency": {"group": "${{ format('ops-checks-{0}', inputs.task) }}"},
        "jobs": {"j": {"concurrency": "ops-checks-facet-reconcile"}},
    }
    assert deadlocks(workflow) == ["j: ops-checks-facet-reconcile"]


def test_the_scheduled_canary_has_its_own_workflow_group() -> None:
    text = (ROOT / ".github" / "workflows" / "ops-checks.yml").read_text(
        encoding="utf-8"
    )
    workflow = yaml.safe_load(text)
    names, _ = workflow_group_names(_group(workflow["concurrency"]))
    assert "infra2-deploy-v2-canary" in names
    assert (
        _group(workflow["jobs"]["deploy-v2-canary"]["concurrency"])
        == "deploy-v2-canary"
    )


def test_a_choice_input_expands_to_its_options() -> None:
    """deploy.yml: format('deploy-v2-{0}', inputs.type) never yields deploy-v2-canary
    while 'canary' is not a type option; once it is, the job deadlocks."""
    workflow = {
        True: {
            "workflow_dispatch": {
                "inputs": {"type": {"type": "choice", "options": ["staging", "prod"]}}
            }
        },
        "concurrency": {
            "group": "${{ format('deploy-v2-{0}', github.event.inputs.type) }}"
        },
        "jobs": {"preflight_canary": {"concurrency": {"group": "deploy-v2-canary"}}},
    }
    assert deadlocks(workflow) == []
    workflow[True]["workflow_dispatch"]["inputs"]["type"]["options"].append("canary")
    assert deadlocks(workflow) == ["preflight_canary: deploy-v2-canary"]
