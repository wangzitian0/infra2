"""Contract for the app-deploy-request repository_dispatch receiver.

Most tests read the workflow. The request-expiry tests (#1232) also run the plan step
of the deploy job with a fake `gh` and a fake receiver."""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from infra2_sdk.deploy import DeployOperation, DeployRequest, DeployType

from libs.deploy.app_deploy_request import (
    PREFLIGHT_CANARY_DEPLOY_TYPES,
    DeployPlan,
    check_request_age,
)

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/app-deploy-request.yml"
SECRET_REF = re.compile(r"\$\{\{\s*secrets\.")
RECEIVER_CLI = "python -m tools.app_deploy_request"
DEPLOY_PLAN_STEP = "Validate and display the side-effect-free plan"


def workflow() -> dict:
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def step(job: dict, name: str) -> dict:
    matches = [item for item in job["steps"] if item.get("name") == name]
    assert len(matches) == 1, f"expected one step named {name!r}"
    return matches[0]


def test_receiver_has_only_the_versioned_repository_dispatch_trigger() -> None:
    definition = workflow()
    triggers = definition["on"]
    assert triggers == {
        "repository_dispatch": {"types": ["app-deploy-request"]},
    }
    assert definition["permissions"] == {"actions": "read", "contents": "read"}
    assert definition["env"]["GITHUB_TOKEN"] == "${{ github.token }}"


def test_receiver_is_one_job_per_concern_and_pays_setup_once_per_job() -> None:
    """truealpha#860: validate/preflight/execute were three jobs, each paying ~17 s of
    runner setup and install. The deploy job now validates and executes; the canary is
    its own job only because the deploy-v2-canary concurrency group must cover the
    canary alone."""
    jobs = workflow()["jobs"]
    assert set(jobs) == {"preflight_canary", "deploy"}
    assert "needs" not in jobs["preflight_canary"]
    assert jobs["deploy"]["needs"] == ["preflight_canary"]
    for job in jobs.values():
        uses = [item.get("uses", "") for item in job["steps"]]
        assert sum(u.startswith("actions/checkout@") for u in uses) == 1
        assert sum(u.startswith("actions/setup-python@") for u in uses) == 1
        runs = [item.get("run", "") for item in job["steps"]]
        assert sum("pip install -e ." in r for r in runs) == 1


def test_the_canary_lock_covers_the_canary_and_not_the_promote() -> None:
    definition = workflow()
    jobs = definition["jobs"]
    assert jobs["preflight_canary"]["concurrency"] == {
        "group": "deploy-v2-canary",
        "cancel-in-progress": "false",
    }
    assert "concurrency" not in jobs["deploy"]
    # the per-(service, deploy_type) run lock still serializes whole requests
    assert definition["concurrency"] == {
        "group": "app-deploy-${{ github.event.client_payload.service }}-"
        "${{ github.event.client_payload.deploy_type }}",
        "cancel-in-progress": "false",
    }


def test_deploy_runs_only_after_a_green_or_skipped_canary_and_never_when_cancelled() -> (
    None
):
    condition = workflow()["jobs"]["deploy"]["if"]
    assert "!cancelled()" in condition
    assert "always()" not in condition
    assert "needs.preflight_canary.result == 'success'" in condition
    assert "needs.preflight_canary.result == 'skipped'" in condition


def test_every_job_validates_without_credentials_before_any_secret_is_in_scope() -> (
    None
):
    """The receiver has always validated a request before any step that can mutate: now
    per step. No workflow- or job-level env carries a secret; in each job the first step
    that runs the receiver CLI is the credential-free plan, and secrets appear only in the
    steps after it."""
    definition = workflow()
    assert not SECRET_REF.search(str(definition["env"]))
    for name, job in definition["jobs"].items():
        assert not SECRET_REF.search(str(job.get("env", {}))), name
        steps = job["steps"]
        plan_index = next(
            i for i, item in enumerate(steps) if RECEIVER_CLI in item.get("run", "")
        )
        plan = steps[plan_index]
        assert plan.get("id") == "plan", name
        assert f"{RECEIVER_CLI} plan" in plan["run"], name
        assert not SECRET_REF.search(str(plan)), name
        for earlier in steps[:plan_index]:
            assert not SECRET_REF.search(str(earlier)), (name, earlier.get("name"))
        secret_steps = [
            i for i, item in enumerate(steps) if SECRET_REF.search(str(item))
        ]
        assert secret_steps and min(secret_steps) > plan_index, name


def test_secrets_are_scoped_to_the_steps_that_use_them() -> None:
    jobs = workflow()["jobs"]
    canary = step(jobs["preflight_canary"], "Canary the exact app and IaC coordinates")
    assert canary["env"]["DOKPLOY_API_KEY"] == "${{ secrets.DOKPLOY_API_KEY }}"
    assert "IAC_WEBHOOK_SECRET" not in str(jobs["preflight_canary"])
    ssh_step = step(jobs["deploy"], "Configure SSH key for the pre-deploy schema gate")
    assert (
        ssh_step["env"]["INFRA2_WATCHDOG_SSH_PRIVATE_KEY"]
        == "${{ secrets.INFRA2_WATCHDOG_SSH_PRIVATE_KEY }}"
    )
    execute = step(jobs["deploy"], "Execute through deploy_v2")
    assert execute["env"]["DOKPLOY_API_KEY"] == "${{ secrets.DOKPLOY_API_KEY }}"
    assert execute["env"]["IAC_WEBHOOK_SECRET"] == "${{ secrets.IAC_WEBHOOK_SECRET }}"
    assert execute["env"]["VPS_HOST"] == "${{ secrets.INFRA2_WATCHDOG_SSH_HOST }}"
    assert (
        execute["env"]["INFRA2_WATCHDOG_SSH_HOST"]
        == "${{ secrets.INFRA2_WATCHDOG_SSH_HOST }}"
    )
    assert (
        execute["env"]["INFRA2_WATCHDOG_SSH_USER"]
        == "${{ secrets.INFRA2_WATCHDOG_SSH_USER }}"
    )
    for job in jobs.values():
        for item in job["steps"]:
            if item in (canary, execute, ssh_step):
                continue
            assert not SECRET_REF.search(str(item)), item.get("name")


def test_receiver_never_checks_out_application_code() -> None:
    body = WORKFLOW.read_text(encoding="utf-8")
    assert "repository:" not in body
    assert body.count(RECEIVER_CLI) == 3
    assert "python -m tools.deploy_v2_canary" in body


_PREFILTER_CLAUSE = re.compile(
    r"github\.event\.client_payload\.(?P<field>deploy_type|operation)\s*"
    r"(?P<op>==|!=)\s*'(?P<value>[^']*)'"
)


def _prefilter_runs(condition: str, *, deploy_type: str, operation: str) -> bool:
    """Evaluate the canary job's `if:` — only the `a == 'x' && b != 'y'` shape it has.

    Any other shape (an `||`, a function call, a new field) fails the parse on purpose:
    whoever changes the pre-filter updates this evaluator and re-proves the mirror."""
    body = condition.strip()
    assert body.startswith("${{") and body.endswith("}}"), condition
    clauses = [clause.strip() for clause in body[3:-2].split("&&")]
    payload = {"deploy_type": deploy_type, "operation": operation}
    result = True
    for clause in clauses:
        match = _PREFILTER_CLAUSE.fullmatch(clause)
        assert match, f"unsupported pre-filter clause {clause!r}"
        equal = payload[match["field"]] == match["value"]
        result = result and (equal if match["op"] == "==" else not equal)
    return result


def _request_shapes() -> list[tuple[str, str, DeployPlan]]:
    """Every (deploy_type, operation) the SDK accepts, as the plan the receiver builds."""
    shapes = []
    for deploy_type in DeployType:
        for operation in DeployOperation:
            try:
                request = DeployRequest.from_dict(
                    {
                        "contract_version": 1,
                        "request_id": "run-12345678",
                        "operation": operation.value,
                        "service": "finance_report/app",
                        "deploy_type": deploy_type.value,
                        "version_ref": "42",
                        "source_repository": "wangzitian0/finance_report",
                        "source_sha": "a" * 40,
                        "evidence": {
                            "source_run_url": "https://github.com/wangzitian0/"
                            "finance_report/actions/runs/100",
                            "source_run_id": "100",
                            "staging_run_url": "https://github.com/wangzitian0/"
                            "finance_report/actions/runs/101",
                            "reviewed_change_url": "https://github.com/wangzitian0/"
                            "finance_report/pull/10",
                        },
                    }
                )
            except ValueError:
                continue  # never reaches a plan: the deploy job's validation refuses it
            plan = DeployPlan(
                request=request, iac_ref="v1.0.0", domain="zitian.party", timeout=600
            )
            shapes.append((deploy_type.value, operation.value, plan))
    return shapes


@pytest.mark.parametrize(
    "deploy_type,operation,plan",
    [pytest.param(*shape, id=f"{shape[0]}-{shape[1]}") for shape in _request_shapes()],
)
def test_canary_pre_filter_mirrors_the_plan_for_every_request_shape(
    deploy_type, operation, plan
) -> None:
    """The canary job is skipped before any plan exists, so its `if:` reads the payload.
    It must agree with DeployPlan.requires_preflight_canary for every deploy type and
    operation the SDK accepts; the receiver also re-checks the plan at run time."""
    condition = workflow()["jobs"]["preflight_canary"]["if"]
    assert (
        _prefilter_runs(condition, deploy_type=deploy_type, operation=operation)
        is plan.requires_preflight_canary
    )


def test_the_pre_filter_mirror_covers_both_outcomes() -> None:
    outcomes = {plan.requires_preflight_canary for *_shape, plan in _request_shapes()}
    assert outcomes == {True, False}
    assert {DeployType.PRODUCTION} == PREFLIGHT_CANARY_DEPLOY_TYPES


def test_a_staging_request_is_not_canaried_and_production_still_is() -> None:
    condition = workflow()["jobs"]["preflight_canary"]["if"]
    assert not _prefilter_runs(condition, deploy_type="staging", operation="deploy")
    assert _prefilter_runs(condition, deploy_type="prod", operation="deploy")
    assert _prefilter_runs(condition, deploy_type="prod", operation="rollback")


def test_both_sides_of_the_pre_filter_are_rechecked_against_the_plan() -> None:
    jobs = workflow()["jobs"]
    canary_plan = step(
        jobs["preflight_canary"], "Validate and display the side-effect-free plan"
    )
    assert "--require-preflight-canary" in canary_plan["run"]
    deploy_plan = step(jobs["deploy"], "Validate and display the side-effect-free plan")
    execute = step(jobs["deploy"], "Execute through deploy_v2")
    for item in (deploy_plan, execute):
        assert item["env"]["PREFLIGHT_CANARY_RESULT"] == (
            "${{ needs.preflight_canary.result }}"
        )
        assert '--preflight-canary-result "$PREFLIGHT_CANARY_RESULT"' in item["run"]


def test_the_iac_coordinate_is_pinned_from_canary_through_execution() -> None:
    """The canary proves one iac_ref; the deploy job's own checkout must still select it,
    and execute must run the ref its plan step displayed."""
    jobs = workflow()["jobs"]
    assert jobs["preflight_canary"]["outputs"] == {
        "iac_ref": "${{ steps.plan.outputs.iac_ref }}"
    }
    canary = step(jobs["preflight_canary"], "Canary the exact app and IaC coordinates")
    assert canary["env"]["VALIDATED_IAC_REF"] == "${{ steps.plan.outputs.iac_ref }}"
    assert '--iac-ref "$VALIDATED_IAC_REF"' in canary["run"]
    deploy_plan = step(jobs["deploy"], "Validate and display the side-effect-free plan")
    assert deploy_plan["env"]["CANARIED_IAC_REF"] == (
        "${{ needs.preflight_canary.outputs.iac_ref }}"
    )
    assert '--expected-iac-ref "$CANARIED_IAC_REF"' in deploy_plan["run"]
    execute = step(jobs["deploy"], "Execute through deploy_v2")
    assert execute["env"]["VALIDATED_IAC_REF"] == "${{ steps.plan.outputs.iac_ref }}"
    assert '--expected-iac-ref "$VALIDATED_IAC_REF"' in execute["run"]


def test_preflight_canary_targets_the_requested_service() -> None:
    """A canary that ignores which service was requested silently validates the wrong
    app: a truealpha/app request canaried finance_report/app's reserved slot instead
    (report-pr-999), because --service was never wired through from the plan."""
    job = workflow()["jobs"]["preflight_canary"]
    plan = step(job, "Validate and display the side-effect-free plan")
    assert 'json.load(sys.stdin)["request"]["service"]' in plan["run"]
    assert 'json.load(sys.stdin)["request"]["version_ref"]' in plan["run"]
    canary = step(job, "Canary the exact app and IaC coordinates")
    assert canary["env"]["VALIDATED_SERVICE"] == "${{ steps.plan.outputs.service }}"
    assert '--service "$VALIDATED_SERVICE"' in canary["run"]
    assert canary["env"]["VALIDATED_VERSION_REF"] == (
        "${{ steps.plan.outputs.version_ref }}"
    )
    assert '--version-ref "$VALIDATED_VERSION_REF"' in canary["run"]


def test_receiver_logs_each_line_as_it_is_printed() -> None:
    assert workflow()["env"]["PYTHONUNBUFFERED"] == "1"


def test_receiver_run_name_matches_what_senders_poll_for() -> None:
    """infra2#537: without a matching run-name, a sender's receipt-polling loop
    (e.g. truealpha's deploy-release.yml, which searches this repo's Actions API for a
    run whose display_title equals "Deploy <service> <deploy_type> <version_ref>
    <source_sha> [<request_id>]") can never find its own receiver run — GitHub's default
    display_title for repository_dispatch is just the workflow name. This run-name must
    reproduce that exact string from the dispatch payload, in that exact order.
    """
    run_name = workflow()["run-name"]
    assert run_name == (
        "Deploy ${{ github.event.client_payload.service }} "
        "${{ github.event.client_payload.deploy_type }} "
        "${{ github.event.client_payload.version_ref }} "
        "${{ github.event.client_payload.source_sha }} "
        "[${{ github.event.client_payload.request_id }}]"
    )


# Request expiry (#1232). The deploy job reads the first creation time of its run and
# gives it to the receiver, which refuses a production request older than its sender's
# wait. A re-run keeps created_at; run_started_at changes on each attempt.
_ENVIRONMENT_CONDITION = re.compile(
    r"\$\{\{\s*(?P<condition>.+?)\s*&&\s*'production'\s*\|\|\s*'staging'\s*\}\}"
)
_AGE_PRE_FILTER = re.compile(
    r"\$\{\{\s*github\.event\.client_payload\.deploy_type\s*==\s*'(?P<value>[^']*)'"
    r"\s*\}\}"
)
_RUN_CREATED_AT = "2026-10-13T03:57:12Z"
_RECORD = 'import json, sys; open(sys.argv[1], "w").write(json.dumps(sys.argv[2:]))'


def test_the_deploy_job_reads_the_request_age_before_any_secret() -> None:
    """The age check is in the credential-free plan step of the deploy job. Its
    production pre-filter is the condition of the job's environment, so the time is
    read for exactly the requests that wait for the owner."""
    job = workflow()["jobs"]["deploy"]
    steps = job["steps"]
    plan_index = next(
        i for i, item in enumerate(steps) if item.get("name") == DEPLOY_PLAN_STEP
    )
    plan = steps[plan_index]
    environment = _ENVIRONMENT_CONDITION.fullmatch(job["environment"])
    assert environment, job["environment"]
    assert plan["env"]["REQUEST_IS_PRODUCTION"] == (
        "${{ " + environment["condition"] + " }}"
    )
    assert not SECRET_REF.search(str(plan))
    secret_steps = [i for i, item in enumerate(steps) if SECRET_REF.search(str(item))]
    assert secret_steps and min(secret_steps) > plan_index


def _executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _recorded(path: Path) -> list[str] | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _run_deploy_plan_step(
    tmp_path: Path, *, production: bool, gh_fails: bool = False
) -> tuple[subprocess.CompletedProcess[str], list[str] | None, list[str] | None]:
    """Run the plan step's own script with a fake `gh` and a fake receiver.

    The fakes record their arguments. The fake receiver prints a minimal plan; every
    other `python` call goes to the real interpreter. Returns the process, the `gh`
    arguments and the receiver arguments (None when the program did not run)."""
    definition = workflow()
    job = definition["jobs"]["deploy"]
    plan = step(job, DEPLOY_PLAN_STEP)
    run = plan["run"]
    assert "${{" not in run, "the harness cannot run a workflow expression"
    declared = [
        plan.get("shell"),
        ((job.get("defaults") or {}).get("run") or {}).get("shell"),
        ((definition.get("defaults") or {}).get("run") or {}).get("shell"),
    ]
    assert not any(declared), f"a `shell:` override is in play ({declared})"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(
        bin_dir / "gh",
        "#!/bin/bash\n"
        f'"$REAL_PYTHON" -c \'{_RECORD}\' "$RECORD_DIR/gh.json" "$@"\n'
        'if [ -n "${GH_FAILS:-}" ]; then echo "HTTP 502" >&2; exit 1; fi\n'
        f"echo '{_RUN_CREATED_AT}'\n",
    )
    _executable(
        bin_dir / "python",
        "#!/bin/bash\n"
        'if [ "${1:-}" = "-m" ] && [ "${2:-}" = "tools.app_deploy_request" ]; then\n'
        f'  "$REAL_PYTHON" -c \'{_RECORD}\' "$RECORD_DIR/receiver.json" "$@"\n'
        """  echo '{"iac_ref": "v1.1.74"}'\n"""
        "  exit 0\n"
        "fi\n"
        'exec "$REAL_PYTHON" "$@"\n',
    )
    step_env = {
        "PREFLIGHT_CANARY_RESULT": "success" if production else "skipped",
        "CANARIED_IAC_REF": "v1.1.74" if production else "",
        "REQUEST_IS_PRODUCTION": "true" if production else "false",
    }
    assert set(plan["env"]) == set(step_env), "the harness must set every step env"
    env = {
        **os.environ,
        **step_env,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "REAL_PYTHON": sys.executable,
        "RECORD_DIR": str(tmp_path),
        "GITHUB_REPOSITORY": "wangzitian0/infra2",
        "GITHUB_RUN_ID": "38344114882",
        "GITHUB_OUTPUT": str(tmp_path / "github_output"),
        "APP_DEPLOY_REQUEST_SENDER": "wangzitian0",
        "INTERNAL_DOMAIN": "zitian.party",
    }
    env.pop("GH_FAILS", None)
    if gh_fails:
        env["GH_FAILS"] = "1"
    script = tmp_path / "step.sh"
    script.write_text(run, encoding="utf-8")
    proc = subprocess.run(
        # GitHub's default shell for `run:` on Linux is `bash -e {0}`.
        ["bash", "-e", str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    return proc, _recorded(tmp_path / "gh.json"), _recorded(tmp_path / "receiver.json")


def test_a_production_request_gives_the_run_creation_time_to_the_receiver(
    tmp_path,
) -> None:
    proc, gh, receiver = _run_deploy_plan_step(tmp_path, production=True)
    assert proc.returncode == 0, proc.stderr
    assert gh == [
        "api",
        "repos/wangzitian0/infra2/actions/runs/38344114882",
        "--jq",
        ".created_at",
    ]
    assert receiver is not None and receiver[:3] == [
        "-m",
        "tools.app_deploy_request",
        "plan",
    ]
    assert "--request-created-at" in receiver
    assert receiver[receiver.index("--request-created-at") + 1] == _RUN_CREATED_AT
    assert (tmp_path / "github_output").read_text(encoding="utf-8") == (
        "iac_ref=v1.1.74\n"
    )


def test_a_staging_request_reads_no_creation_time(tmp_path) -> None:
    """Staging behavior does not change: no API read, and no age limit."""
    proc, gh, receiver = _run_deploy_plan_step(tmp_path, production=False)
    assert proc.returncode == 0, proc.stderr
    assert gh is None, f"gh ran for a staging request: {gh}"
    assert receiver is not None and "--request-created-at" in receiver
    assert receiver[receiver.index("--request-created-at") + 1] == ""


def test_an_unreadable_creation_time_stops_a_production_request(tmp_path) -> None:
    proc, gh, receiver = _run_deploy_plan_step(tmp_path, production=True, gh_fails=True)
    assert gh is not None, "the step did not try to read the creation time"
    assert proc.returncode != 0
    assert receiver is None, "the receiver ran without the creation time"


def _needs_creation_time(plan: DeployPlan) -> bool:
    """True when the receiver refuses this request without a creation time."""
    try:
        check_request_age(plan.request, created_at="", now=datetime.now(timezone.utc))
    except ValueError:
        return True
    return False


@pytest.mark.parametrize(
    "deploy_type,operation,plan",
    [pytest.param(*shape, id=f"{shape[0]}-{shape[1]}") for shape in _request_shapes()],
)
def test_the_age_pre_filter_mirrors_the_receiver_for_every_request_shape(
    deploy_type, operation, plan
) -> None:
    """The step reads the time only when the payload says production. The receiver
    must ask for the time for exactly those requests, or a request fails closed."""
    plan_step = step(workflow()["jobs"]["deploy"], DEPLOY_PLAN_STEP)
    pre_filter = _AGE_PRE_FILTER.fullmatch(plan_step["env"]["REQUEST_IS_PRODUCTION"])
    assert pre_filter, plan_step["env"]["REQUEST_IS_PRODUCTION"]
    assert (deploy_type == pre_filter["value"]) is _needs_creation_time(plan)


def test_the_age_pre_filter_mirror_covers_both_outcomes() -> None:
    outcomes = {_needs_creation_time(plan) for *_shape, plan in _request_shapes()}
    assert outcomes == {True, False}
