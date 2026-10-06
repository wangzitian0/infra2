"""A job must not share a concurrency group with its own workflow run (#1086).

GitHub cannot start a job whose ``concurrency.group`` equals the group its
workflow run already holds. The job ends ``failure`` with no runner and no step,
so an alert step inside it never runs. #993 renamed the deploy_v2 canary job's
group to ``deploy-v2-canary``, the name the ``37 6 * * *`` schedule gives its
whole run, and the daily scheduled canary stopped starting without a page.

Sharing one job-level group across workflows is intended (it serializes every
canary on the reserved slot). Only a match within one workflow deadlocks.

The check over-approximates: every quoted literal in the workflow-level
expression counts as a possible run group, a ``format()`` or text-plus-``${{ }}``
group counts as an open prefix unless its value is a choice input, and a shape
it cannot bound is a finding, not a pass.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))

_EXPR = re.compile(r"\$\{\{(.*?)\}\}", re.S)
_LITERAL = re.compile(r"'([^']*)'")
_FORMAT = re.compile(r"format\(\s*'([^']*)'\s*,([^)]*)\)")
_INPUT = re.compile(r"^\s*(?:github\.event\.)?inputs\.(\w+)\s*$")


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
) -> tuple[set[str], set[str], bool]:
    """(names, prefixes, open) a workflow-level group can evaluate to.

    ``open`` means a value the check cannot bound (for example ``${{ github.ref }}``
    or a ``format()`` that starts with its placeholder)."""
    workflow = workflow or {}
    if "${{" not in group:
        return {group}, set(), False
    names: set[str] = set()
    prefixes: set[str] = set()
    is_open = False
    text_head = group.split("${{", 1)[0]
    expressions = _EXPR.findall(group)
    if (
        text_head
        or len(expressions) > 1
        or group.strip() != "${{" + expressions[0] + "}}"
    ):
        # Text mixed with expressions: the static head is a prefix, unless the only
        # expression is a choice input that can be expanded.
        match = _INPUT.match(expressions[0]) if len(expressions) == 1 else None
        options = _choices(workflow, match.group(1)) if match else None
        tail = group.split("}}", 1)[1] if len(expressions) == 1 else None
        if options is not None and tail is not None and "${{" not in tail:
            names.update(f"{text_head}{o}{tail}" for o in options)
        elif text_head:
            prefixes.add(text_head)
        else:
            is_open = True
        return names, prefixes, is_open
    expression = expressions[0]
    for pattern, args in _FORMAT.findall(expression):
        head = pattern.split("{", 1)[0]
        arg_list = [a for a in args.split(",") if a.strip()]
        match = _INPUT.match(arg_list[0]) if len(arg_list) == 1 else None
        options = _choices(workflow, match.group(1)) if match else None
        if options is not None and pattern.count("{") == 1:
            names.update(pattern.replace("{0}", o) for o in options)
        elif head:
            prefixes.add(head)
        else:
            is_open = True
    names.update(_LITERAL.findall(_FORMAT.sub("", expression)))
    # A context value outside a comparison or format() can be the result itself.
    stripped = _FORMAT.sub("", expression)
    stripped = re.sub(r"[\w.]+\s*(==|!=)\s*'[^']*'", "", stripped)
    stripped = re.sub(r"'[^']*'", "", stripped)
    if re.search(r"\b(github|inputs|env|vars|steps|needs|matrix)\.[\w.]+", stripped):
        is_open = True
    return names, prefixes, is_open


def deadlocks(workflow: dict) -> list[str]:
    top = _group(workflow.get("concurrency"))
    if not top:
        return []
    names, prefixes, is_open = workflow_group_names(top, workflow)
    found = []
    for job_id, job in (workflow.get("jobs") or {}).items():
        group = _group((job or {}).get("concurrency"))
        if group is None:
            continue
        if group == top:
            found.append(f"{job_id}: {group}")
        elif "${{" in group:
            found.append(f"{job_id}: cannot bound the job group {group!r}")
        elif group in names or any(group.startswith(p) for p in prefixes):
            found.append(f"{job_id}: {group}")
        elif is_open:
            found.append(f"{job_id}: cannot bound the workflow group {top!r}")
    return found


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_job_shares_a_concurrency_group_with_its_workflow(path: Path) -> None:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert deadlocks(workflow) == []


def _wf(top, job_group, options=None):
    workflow = {
        "concurrency": {"group": top},
        "jobs": {"j": {"concurrency": {"group": job_group}}},
    }
    if options is not None:
        workflow[True] = {
            "workflow_dispatch": {
                "inputs": {"type": {"type": "choice", "options": options}}
            }
        }
    return workflow


@pytest.mark.parametrize(
    "top, job_group",
    [
        # #993's shape.
        (
            "${{ github.event_name == 'schedule' && github.event.schedule == '37 6 * * *'"
            " && 'deploy-v2-canary' || format('ops-checks-push-{0}', github.sha) }}",
            "deploy-v2-canary",
        ),
        # The last fallback value of an || chain.
        (
            "${{ github.event_name == 'push' && 'a' || 'deploy-v2-canary' }}",
            "deploy-v2-canary",
        ),
        ("${{ inputs.g || 'deploy-v2-canary' }}", "deploy-v2-canary"),
        # format() prefixes.
        ("${{ format('ops-checks-{0}', inputs.task) }}", "ops-checks-facet-reconcile"),
        ("${{ format('deploy-v2-{0}-{1}', inputs.a, inputs.b) }}", "deploy-v2-x-y"),
        # Text mixed with an expression.
        ("deploy-v2-${{ inputs.type }}", "deploy-v2-canary"),
        # Identical expressions at both levels, the common copy-paste deadlock.
        (
            "${{ github.workflow }}-${{ github.ref }}",
            "${{ github.workflow }}-${{ github.ref }}",
        ),
        # Shapes the check cannot bound are findings, not passes.
        ("${{ format('{0}-canary', inputs.x) }}", "anything"),
        ("${{ github.ref }}", "deploy-v2-canary"),
        ("${{ github.workflow }}", "${{ github.job }}"),
    ],
)
def test_each_deadlock_shape_is_found(top, job_group) -> None:
    assert deadlocks(_wf(top, job_group)) != []


def test_a_choice_input_expands_to_its_options() -> None:
    """deploy.yml: format('deploy-v2-{0}', inputs.type) never yields deploy-v2-canary
    while 'canary' is not a type option; once it is, the job deadlocks."""
    top = "${{ format('deploy-v2-{0}', github.event.inputs.type) }}"
    assert deadlocks(_wf(top, "deploy-v2-canary", ["staging", "prod"])) == []
    assert deadlocks(_wf(top, "deploy-v2-canary", ["staging", "canary"])) != []
    mixed = "deploy-v2-${{ inputs.type }}"
    assert deadlocks(_wf(mixed, "deploy-v2-canary", ["staging", "prod"])) == []
    assert deadlocks(_wf(mixed, "deploy-v2-canary", ["canary"])) != []


def test_the_scheduled_canary_has_its_own_workflow_group() -> None:
    text = (ROOT / ".github" / "workflows" / "ops-checks.yml").read_text(
        encoding="utf-8"
    )
    workflow = yaml.safe_load(text)
    names, _, _ = workflow_group_names(_group(workflow["concurrency"]), workflow)
    assert "infra2-deploy-v2-canary" in names
    assert (
        _group(workflow["jobs"]["deploy-v2-canary"]["concurrency"])
        == "deploy-v2-canary"
    )
