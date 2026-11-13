"""#1646: the release train has three jobs: `pre`, `operation`, `post`.

The owner is asked to approve once, on `operation`, and only after `pre` passed. `pre` holds no
secret and no environment. `post` runs the gate hooks, makes no deploy call and cannot push the
production marker, so a production promotion needs one approval. An `alert` job reports the
state of the three. The rules below run on the real workflow and on mutated copies of it.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest
import yaml

from common.gate import production_contract as contract

ROOT = Path(__file__).resolve().parents[2]
NAME = "reconcile-iac-inputs.yml"
WORKFLOW = ROOT / ".github" / "workflows" / NAME
ENVIRONMENT = "${{ inputs.promote_prod && 'production' || 'staging' }}"
SETUP = "./.github/actions/train-setup"


def _load() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _text(node: object) -> str:
    return yaml.safe_dump(node)


def _secrets(node: object) -> set[str]:
    return set(re.findall(r"secrets\.([A-Za-z0-9_]+)", _text(node)))


def _writes_contents(workflow: dict, job: dict) -> bool:
    permissions = job.get("permissions", workflow.get("permissions"))
    return permissions == "write-all" or (
        isinstance(permissions, dict) and permissions.get("contents") == "write"
    )


def violations(workflow: dict) -> list[str]:
    """Every way the three-job layout can be broken. Empty means the layout holds."""
    found: list[str] = []
    jobs = workflow["jobs"]
    for name in ("pre", "operation", "post"):
        if name not in jobs:
            return [f"job {name} is missing"]
    pre, operation, post = jobs["pre"], jobs["operation"], jobs["post"]
    if "environment" in pre:
        found.append("pre declares an environment")
    if _secrets(pre):
        found.append(f"pre reads secrets {sorted(_secrets(pre))}")
    if operation.get("needs") != "pre":
        found.append(f"operation needs {operation.get('needs')!r}, not 'pre'")
    if post.get("needs") != "operation":
        found.append(f"post needs {post.get('needs')!r}, not 'operation'")
    if operation.get("environment") != ENVIRONMENT:
        found.append("the environment expression is not on operation")
    for name, job in jobs.items():
        if name != "operation" and "environment" in job:
            found.append(f"{name} declares an environment: a second owner approval")
        if name != "operation" and _writes_contents(workflow, job):
            found.append(f"{name} holds contents: write")
    if workflow.get("permissions") != {"contents": "read"}:
        found.append("the workflow default is not read-only: holds contents: write")
    if not _writes_contents(workflow, operation):
        found.append("operation cannot push the production marker")
    for name in ("pre", "operation", "post"):
        if jobs[name].get("permissions") is None:
            found.append(f"{name} sets no permissions of its own")
    allowed = contract.UNGATED_SECRET_LIMITS[(NAME, "post")]
    if _secrets(post) - allowed:
        found.append(f"post reads secrets {sorted(_secrets(post) - allowed)}")
    for name in ("pre", "operation", "post", "alert"):
        uses = [s.get("uses") for s in jobs[name]["steps"]]
        if SETUP not in uses:
            found.append(f"{name} does not use the train-setup action")
    return found


def test_the_workflow_has_three_train_jobs_and_one_gated_environment() -> None:
    """AC1 (#1646): jobs `pre`, `operation`, `post`; `pre` has no `environment` key;
    `operation.needs == "pre"`; `post.needs == "operation"`; the `environment` expression
    sits on `operation`."""
    workflow = _load()
    assert violations(workflow) == []
    jobs = workflow["jobs"]
    assert {"pre", "operation", "post"} <= set(jobs)
    assert "environment" not in jobs["pre"]
    assert jobs["operation"]["needs"] == "pre"
    assert jobs["post"]["needs"] == "operation"
    assert jobs["operation"]["environment"] == ENVIRONMENT
    assert [name for name, job in jobs.items() if "environment" in job] == ["operation"]


def _mutate(path: list[str | int], value: object = None) -> dict:
    """The real workflow with one edit: set `path` to `value`, or delete it when None."""
    workflow = copy.deepcopy(_load())
    node = workflow
    for key in path[:-1]:
        node = node[key]
    if value is None:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return workflow


@pytest.mark.parametrize(
    ("mutation", "needle"),
    [
        # `environment` on `pre`: the owner would be asked before the guards ran
        (
            _mutate(["jobs", "pre", "environment"], ENVIRONMENT),
            "pre declares an environment",
        ),
        # a secret read in `pre`
        (
            _mutate(["jobs", "pre", "env", "X"], "${{ secrets.DOKPLOY_API_KEY }}"),
            "pre reads secrets ['DOKPLOY_API_KEY']",
        ),
        # the environment expression moves from `operation` to `post`
        (
            _mutate(["jobs", "post", "environment"], ENVIRONMENT),
            "second owner approval",
        ),
        (_mutate(["jobs", "operation", "environment"]), "not on operation"),
        # the chain
        (_mutate(["jobs", "operation", "needs"], []), "operation needs []"),
        (_mutate(["jobs", "post", "needs"], ["pre", "operation"]), "post needs"),
        # `post` pushes the marker: a production job with its own approval
        (
            _mutate(["jobs", "post", "permissions"], {"contents": "write"}),
            "post holds contents: write",
        ),
        (_mutate(["permissions"], {"contents": "write"}), "not read-only"),
        # `post` reads a deploy secret
        (
            _mutate(["jobs", "post", "env", "X"], "${{ secrets.IAC_WEBHOOK_SECRET }}"),
            "post reads secrets ['IAC_WEBHOOK_SECRET']",
        ),
        # a job that stops using the shared setup
        (
            _mutate(["jobs", "post", "steps", 1, "uses"], "actions/setup-python@v6"),
            "post does not use the train-setup action",
        ),
    ],
    ids=[
        "environment-on-pre",
        "secret-in-pre",
        "environment-on-post",
        "environment-off-operation",
        "operation-needs",
        "post-needs",
        "post-pushes",
        "workflow-pushes",
        "post-deploy-secret",
        "no-shared-setup",
    ],
)
def test_the_layout_rules_fail_under_each_mutation(mutation, needle) -> None:
    assert any(needle in item for item in violations(mutation)), violations(mutation)


# -- when each job runs -----------------------------------------------------------------


def _runs(expression: str, **context: object) -> bool:
    """Would GitHub run a job with this `if:`? Evaluates the forms that the train uses."""
    expr = expression.strip().removeprefix("${{").removesuffix("}}").strip()
    expr = re.sub(r"needs\.(\w+)\.result", r'_needs["\1"]', expr)
    expr = re.sub(r"inputs\.(\w+)", r'_inputs.get("\1", "")', expr)
    expr = expr.replace("github.event_name", "_event").replace("!cancelled()", "True")
    expr = expr.replace("&&", " and ").replace("||", " or ")
    expr = re.sub(r"!(?!=)", " not ", expr)
    return bool(
        eval(  # noqa: S307 - a checked-in workflow condition
            expr,
            {"__builtins__": {}},
            {
                "_needs": context.get("needs", {}),
                "_inputs": context.get("inputs", {}),
                "_event": context.get("event", "push"),
            },
        )
    )


def _if(job: str) -> str:
    return _load()["jobs"][job]["if"]


DEPLOY_RUNS = [
    ("a tag push", "push", {}),
    ("a dispatch of all phases", "workflow_dispatch", {"phase": "all"}),
]


@pytest.mark.parametrize(("label", "event", "inputs"), DEPLOY_RUNS)
def test_a_deploy_run_asks_the_owner_only_after_pre_passed(
    label, event, inputs
) -> None:
    """`operation` is the job with the environment gate. It never starts after a failed,
    cancelled or skipped `pre` in a deploy run, so no approval is asked of a refused run."""
    run = _if("operation")
    ctx = {"event": event, "inputs": inputs}
    assert _runs(_if("pre"), **ctx)
    assert _runs(run, needs={"pre": "success"}, **ctx)
    for result in ("failure", "cancelled", "skipped"):
        assert not _runs(run, needs={"pre": result}, **ctx), (label, result)


def test_a_run_that_is_not_a_deploy_run_skips_pre_and_still_runs_operation() -> None:
    for event, inputs in (
        ("schedule", {}),
        ("workflow_dispatch", {"sync_app_releases": True}),
        ("workflow_dispatch", {"run_hook_slot": "smoke"}),
    ):
        ctx = {"event": event, "inputs": inputs}
        assert not _runs(_if("pre"), **ctx), (event, inputs)
        assert _runs(_if("operation"), needs={"pre": "skipped"}, **ctx)
        assert not _runs(_if("operation"), needs={"pre": "failure"}, **ctx)
        assert not _runs(_if("post"), needs={"operation": "success"}, **ctx), (
            "a sync or a hook slot run has no post phase"
        )


def test_post_follows_a_passed_operation_and_can_run_alone_on_a_dispatch() -> None:
    run = _if("post")
    push = {"event": "push", "inputs": {}}
    assert _runs(run, needs={"operation": "success"}, **push)
    for result in ("failure", "cancelled", "skipped"):
        assert not _runs(run, needs={"operation": result}, **push), result
    alone = {"event": "workflow_dispatch", "inputs": {"phase": "post"}}
    assert not _runs(_if("pre"), **alone)
    assert not _runs(_if("operation"), needs={"pre": "skipped"}, **alone)
    assert _runs(run, needs={"operation": "skipped"}, **alone)
    # a skipped operation is not an excuse in any other run
    assert not _runs(
        run,
        needs={"operation": "skipped"},
        event="workflow_dispatch",
        inputs={"phase": "all"},
    )


def test_phase_pre_runs_the_guards_and_nothing_else() -> None:
    only_pre = {"event": "workflow_dispatch", "inputs": {"phase": "pre"}}
    assert _runs(_if("pre"), **only_pre)
    assert not _runs(_if("operation"), needs={"pre": "success"}, **only_pre)
    assert not _runs(_if("post"), needs={"operation": "success"}, **only_pre)


def test_the_phase_input_offers_all_pre_and_post() -> None:
    phase = _load()["on"]["workflow_dispatch"]["inputs"]["phase"]
    assert phase["type"] == "choice" and phase["default"] == "all"
    assert phase["options"] == [
        "all",
        "pre",
        "post",
    ]  # no `operation` alone: it skips `pre`


# -- the production contract ------------------------------------------------------------

# The `reconcile` job of the workflow on main before #1646 (fabf7232): one job, gated by the
# environment, held every secret below and `contents: write`.
BEFORE_SECRETS = frozenset(
    {
        "IAC_WEBHOOK_SECRET",
        "DOKPLOY_API_KEY",
        "INFRA2_WATCHDOG_SSH_HOST",
        "INFRA2_WATCHDOG_SSH_USER",
        "INFRA2_WATCHDOG_SSH_PRIVATE_KEY",
        "SIGNOZ_API_KEY",
        "TA_EMAIL",
        "TA_MEMBER_EMAIL",
        "TA_PASSWORD",
        "TA_MCP_TOKEN",
        "INFRA2_OUT_OF_BAND_ALERT_DELIVERY_MODE",
        "INFRA2_OUT_OF_BAND_FEISHU_WEBHOOK_URL",
        "INFRA2_OUT_OF_BAND_FEISHU_APP_ID",
        "INFRA2_OUT_OF_BAND_FEISHU_APP_SECRET",
        "INFRA2_OUT_OF_BAND_FEISHU_CHAT_ID",
        "INFRA2_OUT_OF_BAND_FEISHU_API_BASE",
        "INFRA2_REPORTS_FEISHU_APP_ID",
        "INFRA2_REPORTS_FEISHU_APP_SECRET",
        "INFRA2_REPORTS_FEISHU_CHAT_ID",
    }
)
DEPLOY_SECRETS = frozenset(
    {
        "IAC_WEBHOOK_SECRET",
        "DOKPLOY_API_KEY",
        "INFRA2_WATCHDOG_SSH_HOST",
        "INFRA2_WATCHDOG_SSH_USER",
        "INFRA2_WATCHDOG_SSH_PRIVATE_KEY",
        "SIGNOZ_API_KEY",
    }
)


def test_the_contract_gates_a_production_run_through_the_same_single_job() -> None:
    """Before #1646 the contract gated the production run through one job, `reconcile`, which
    held every secret and `contents: write`. After it, the contract gates the run through one
    job, `operation`, with the same condition. No deploy secret moves into an ungated job: the
    two ungated jobs read only the named secrets of the alert and of the staging gate."""
    workflow = _load()
    jobs = workflow["jobs"]
    # BEFORE: one gated job (the table of the old tree), and its condition was the same
    assert contract.GATED_JOBS[(NAME, "operation")] == "inputs.promote_prod"
    assert (NAME, "reconcile") not in contract.GATED_JOBS
    assert [k for k in contract.GATED_JOBS if k[0] == NAME] == [(NAME, "operation")]
    # AFTER: the secrets of the old job are read by `operation`, `post` and `alert` only
    reads = {name: contract._secrets_read(job) for name, job in jobs.items()}
    assert reads["pre"] == set()
    assert set().union(*reads.values()) == set(BEFORE_SECRETS)
    # every deploy secret and `contents: write` stay in the gated job
    assert DEPLOY_SECRETS <= reads["operation"]
    for name in ("pre", "post", "alert"):
        assert not DEPLOY_SECRETS & reads[name], name
        assert not contract._writes_contents(workflow, jobs[name]), name
    assert contract._writes_contents(workflow, jobs["operation"])
    # the two ungated jobs read only what their limit names, and nothing is unclassified
    for name in ("post", "alert"):
        assert reads[name] <= contract.UNGATED_SECRET_LIMITS[(NAME, name)], name
        assert (NAME, name) in contract.UNGATED_JOBS
    assert (NAME, "pre") not in contract.UNGATED_JOBS
    assert contract.contract_errors(
        {NAME: workflow}, contract.GATED_JOBS, contract.UNGATED_JOBS
    ) == [
        f"{k[0]}:{k[1]} is named in the contract and does not exist"
        for k in sorted(
            (set(contract.GATED_JOBS) | set(contract.UNGATED_JOBS))
            - {(NAME, j) for j in jobs}
        )
    ]
    assert (
        contract.workflow_contract_failures(
            {NAME: WORKFLOW.read_text(encoding="utf-8")}
        )
        != []
    )  # a lone file lacks the other jobs
    texts = {
        p.name: p.read_text(encoding="utf-8")
        for p in (ROOT / ".github/workflows").glob("*.y*ml")
    }
    assert contract.workflow_contract_failures(texts) == []


def test_a_deploy_secret_added_to_post_or_alert_fails_the_contract() -> None:
    for job in ("post", "alert"):
        mutated = _mutate(["jobs", job, "env", "X"], "${{ secrets.DOKPLOY_API_KEY }}")
        errors = contract.contract_errors({NAME: mutated}, {}, contract.UNGATED_JOBS)
        assert (
            f"{NAME}:{job} is ungated and reads ['DOKPLOY_API_KEY'] outside its secret limit"
            in errors
        )


def test_an_edit_that_gives_post_contents_write_fails_the_contract() -> None:
    mutated = _mutate(["jobs", "post", "permissions"], {"contents": "write"})
    errors = contract.contract_errors({NAME: mutated}, {}, contract.UNGATED_JOBS)
    assert f"{NAME}:post is ungated and holds permission contents: write" in errors


def test_a_pre_that_reads_a_secret_is_unclassified_and_fails_the_contract() -> None:
    """`pre` is in neither table: it must read no secret and hold no `contents: write`."""
    mutated = _mutate(["jobs", "pre", "env", "X"], "${{ secrets.DOKPLOY_API_KEY }}")
    errors = contract.contract_errors(
        {NAME: mutated}, contract.GATED_JOBS, contract.UNGATED_JOBS
    )
    assert any(
        e.startswith(f"{NAME}:pre holds production credentials ['DOKPLOY_API_KEY']")
        for e in errors
    )
    pushing = _mutate(["jobs", "pre", "permissions"], {"contents": "write"})
    errors = contract.contract_errors(
        {NAME: pushing}, contract.GATED_JOBS, contract.UNGATED_JOBS
    )
    assert any(
        "pre holds production credentials ['permission contents: write']" in e
        for e in errors
    )


def test_a_production_run_passes_post_no_hook_secret() -> None:
    """The owner approved `operation` alone, so `post` of a production run reads no secret:
    each hook secret of `post` is the empty string when `promote_prod` is true."""
    env = _load()["jobs"]["post"]["env"]
    hook = {k: v for k, v in env.items() if "secrets." in str(v)}
    assert set(hook) == {"TA_EMAIL", "TA_MEMBER_EMAIL", "TA_PASSWORD", "TA_MCP_TOKEN"}
    for name, value in hook.items():
        assert value == "${{ !inputs.promote_prod && secrets.%s || '' }}" % name


def test_every_job_has_its_own_permissions_and_the_workflow_default_is_read_only() -> (
    None
):
    workflow = _load()
    assert workflow["permissions"] == {"contents": "read"}
    for name, job in workflow["jobs"].items():
        assert isinstance(job.get("permissions"), dict), name
    assert workflow["jobs"]["pre"]["permissions"] == {
        "contents": "read",
        "actions": "read",
    }
    assert workflow["jobs"]["post"]["permissions"] == {
        "contents": "read",
        "actions": "read",
    }


def test_the_alert_job_merges_the_results_of_the_three_jobs() -> None:
    """The failed job writes `alert_text`, a deploy job writes `hook_results`; the merge step
    gives the alert steps one `reconcile-result.json` (a jq filter, run here when jq exists)."""
    import json
    import shutil
    import subprocess

    if shutil.which("jq") is None:
        pytest.skip("jq is not installed")
    script = next(
        s["run"]
        for s in _load()["jobs"]["alert"]["steps"]
        if s.get("name") == "Merge the results of the train jobs"
    )
    filter_ = re.search(r"jq -s '(.*?)' \"\$\{files\[@\]\}\"", script, re.S).group(1)

    def merged(*results: dict) -> dict:
        out = subprocess.run(
            ["jq", "-s", filter_],
            input="\n".join(json.dumps(r) for r in results),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return json.loads(out)

    op = {"alert_text": None, "hook_results": [{"slot": "smoke", "status": "pass"}]}
    post = {
        "alert_text": "hook failed",
        "hook_results": [{"slot": "staging_gate", "status": "fail"}],
    }
    pre = {"alert_text": None, "hook_results": []}
    assert merged(op, post, pre) == {
        "alert_text": "hook failed",
        "hook_results": [
            {"slot": "smoke", "status": "pass"},
            {"slot": "staging_gate", "status": "fail"},
        ],
    }
    assert merged(pre) == {"alert_text": None, "hook_results": []}
