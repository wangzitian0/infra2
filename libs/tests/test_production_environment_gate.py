"""Contract: a job that holds a production credential declares its environment (#1125, #1035).

A GitHub environment with a required reviewer is the only mechanism that stops a
production job before it runs. A job that reads a production credential must declare
`environment:`, or the contract must name it as not production, with a reason.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = ROOT / ".github" / "workflows"

# A job that reads one of these secrets can reach a production host or API.
PRODUCTION_SECRETS = (
    "CF_API_TOKEN",
    "CF_WORKER_API_TOKEN",
    "DOKPLOY_API_KEY",
    "IAC_WEBHOOK_SECRET",
    "INFRA2_WATCHDOG_SSH_HOST",
    "INFRA2_WATCHDOG_SSH_PRIVATE_KEY",
    "SIGNOZ_API_KEY",
)

# Jobs that can change production. Value: the text that the production condition must
# contain, or None when the job always runs against production.
GATED_JOBS: dict[tuple[str, str], str | None] = {
    ("deploy.yml", "deploy"): "inputs.type == 'prod'",
    ("deploy.yml", "bootstrap"): None,
    ("reconcile-iac-inputs.yml", "reconcile"): "inputs.promote_prod",
    ("app-deploy-request.yml", "deploy"): "client_payload.deploy_type == 'prod'",
    ("apply-observability.yml", "apply"): None,
    ("deploy-cloudflare-watchdog.yml", "deploy"): None,
}

# Jobs that read a production credential and do not need owner approval, with the reason.
UNGATED_JOBS: dict[tuple[str, str], str] = {
    ("deploy.yml", "preflight_canary"): "canary on the throwaway slot, not production",
    (
        "app-deploy-request.yml",
        "preflight_canary",
    ): "canary on the throwaway slot, not production",
    ("deploy-report-main.yml", "deploy"): "deploys the preview slot, not production",
    ("preview-teardown.yml", "teardown"): "removes a preview slot, not production",
    ("ops-checks.yml", "audit"): "scheduled read-only audit",
    ("ops-checks.yml", "watchdog"): "scheduled read-only probe",
    ("ops-checks.yml", "digest"): "scheduled read-only report",
    (
        "ops-checks.yml",
        "deploy-v2-canary",
    ): "canary on the throwaway slot, not production",
    (
        "ops-checks.yml",
        "preview-leak-check",
    ): "scheduled read-only check of preview slots",
    ("ops-checks.yml", "vault-self-refresh-audit"): "scheduled read-only audit",
    ("ops-checks.yml", "secrets-reconcile"): "scheduled check that reports drift",
    # These two jobs write to the production host on a schedule. A review gate would stall
    # the schedule. The owner decides their class in the follow-up issue of #1125.
    (
        "ops-checks.yml",
        "host-hygiene-schedule",
    ): "scheduled standing automation, class pending",
    (
        "ops-checks.yml",
        "facet-reconcile",
    ): "scheduled standing automation, class pending",
}

_GATED_EXPRESSION = re.compile(
    r"^\$\{\{\s*(?P<condition>.+?)\s*&&\s*'production'\s*\|\|\s*'staging'\s*\}\}$"
)


def _load_workflows(directory: Path) -> dict[str, dict]:
    workflows = {}
    for path in sorted(directory.glob("*.y*ml")):
        workflows[path.name] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return workflows


def _secrets_read(job: dict) -> set[str]:
    text = yaml.dump(job)
    return {
        name for name in PRODUCTION_SECRETS if re.search(rf"secrets\.{name}\b", text)
    }


def _writes_contents(workflow: dict, job: dict) -> bool:
    """A token with `contents: write` can push a tag, such as a production marker."""
    permissions = (
        job["permissions"] if "permissions" in job else workflow.get("permissions")
    )
    if permissions == "write-all":
        return True
    return isinstance(permissions, dict) and permissions.get("contents") == "write"


def _production_credentials(workflow: dict, job: dict) -> list[str]:
    credentials = sorted(_secrets_read(job))
    if _writes_contents(workflow, job):
        credentials.append("permission contents: write")
    return credentials


def _environment_name(job: dict) -> str | None:
    environment = job.get("environment")
    if isinstance(environment, dict):
        environment = environment.get("name")
    return environment if isinstance(environment, str) else None


def _environment_error(
    key: tuple[str, str], job: dict, selector: str | None
) -> str | None:
    label = f"{key[0]}:{key[1]}"
    environment = _environment_name(job)
    if environment is None:
        return f"{label} can change production and declares no environment"
    if selector is None:
        if environment != "production":
            return f"{label} always runs against production; environment is {environment!r}"
        return None
    match = _GATED_EXPRESSION.match(environment)
    if match is None:
        return f"{label} environment must be '${{{{ <condition> && 'production' || 'staging' }}}}'"
    if selector not in match.group("condition"):
        return f"{label} environment condition lacks {selector!r}"
    return None


def contract_errors(
    workflows: dict[str, dict],
    gated: dict[tuple[str, str], str | None],
    ungated: dict[tuple[str, str], str],
) -> list[str]:
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for workflow_name, workflow in workflows.items():
        for job_name, job in (workflow.get("jobs") or {}).items():
            key = (workflow_name, job_name)
            seen.add(key)
            if key in gated:
                error = _environment_error(key, job, gated[key])
                if error:
                    errors.append(error)
            elif key in ungated:
                if not ungated[key].strip():
                    errors.append(
                        f"{workflow_name}:{job_name} is ungated without a reason"
                    )
                if _writes_contents(workflow, job):
                    errors.append(
                        f"{workflow_name}:{job_name} is ungated and holds permission contents: write"
                    )
            elif credentials := _production_credentials(workflow, job):
                errors.append(
                    f"{workflow_name}:{job_name} holds production credentials "
                    f"{credentials} and is in neither GATED_JOBS nor UNGATED_JOBS"
                )
    for key in sorted((set(gated) | set(ungated)) - seen):
        errors.append(f"{key[0]}:{key[1]} is named in the contract and does not exist")
    for key in sorted(set(gated) & set(ungated)):
        errors.append(f"{key[0]}:{key[1]} is both gated and ungated")
    return errors


def test_repository_workflows_satisfy_the_production_environment_contract():
    workflows = _load_workflows(WORKFLOWS_DIR)
    assert workflows, "no workflow was read"
    assert contract_errors(workflows, GATED_JOBS, UNGATED_JOBS) == []


def test_the_contract_governs_every_production_credential_job_in_the_tree():
    workflows = _load_workflows(WORKFLOWS_DIR)
    reading = {
        (name, job_name)
        for name, workflow in workflows.items()
        for job_name, job in (workflow.get("jobs") or {}).items()
        if _production_credentials(workflow, job)
    }
    assert reading, "no job holds a production credential; the credential list is stale"
    assert reading <= set(GATED_JOBS) | set(UNGATED_JOBS)


def _workflow(job_body: dict) -> dict[str, dict]:
    return {"w.yml": {"jobs": {"j": job_body}}}


_READS_SECRET = {
    "steps": [{"run": "x", "env": {"K": "${{ secrets.DOKPLOY_API_KEY }}"}}]
}
_KEY = ("w.yml", "j")


def test_a_new_job_that_reads_a_production_secret_is_reported():
    errors = contract_errors(_workflow(_READS_SECRET), {}, {})
    assert len(errors) == 1
    assert "w.yml:j holds production credentials ['DOKPLOY_API_KEY']" in errors[0]


def test_a_gated_job_without_an_environment_is_reported():
    errors = contract_errors(_workflow(_READS_SECRET), {_KEY: None}, {})
    assert errors == ["w.yml:j can change production and declares no environment"]


def test_an_always_production_job_with_the_staging_environment_is_reported():
    job = {**_READS_SECRET, "environment": "staging"}
    errors = contract_errors(_workflow(job), {_KEY: None}, {})
    assert errors == [
        "w.yml:j always runs against production; environment is 'staging'"
    ]


def test_a_conditional_environment_must_name_the_production_condition():
    wrong = {
        **_READS_SECRET,
        "environment": "${{ inputs.other && 'production' || 'staging' }}",
    }
    errors = contract_errors(_workflow(wrong), {_KEY: "inputs.type == 'prod'"}, {})
    assert errors == ["w.yml:j environment condition lacks \"inputs.type == 'prod'\""]


def test_a_conditional_environment_with_a_literal_is_reported():
    literal = {**_READS_SECRET, "environment": "production"}
    errors = contract_errors(_workflow(literal), {_KEY: "inputs.type == 'prod'"}, {})
    assert len(errors) == 1 and "environment must be" in errors[0]


def test_a_correct_conditional_environment_passes():
    good = {
        **_READS_SECRET,
        "environment": "${{ inputs.type == 'prod' && 'production' || 'staging' }}",
    }
    assert contract_errors(_workflow(good), {_KEY: "inputs.type == 'prod'"}, {}) == []


def test_a_contract_entry_for_a_missing_job_is_reported():
    errors = contract_errors(_workflow({"steps": []}), {("w.yml", "gone"): None}, {})
    assert errors == ["w.yml:gone is named in the contract and does not exist"]


def test_an_ungated_job_needs_a_reason():
    errors = contract_errors(_workflow(_READS_SECRET), {}, {_KEY: "  "})
    assert errors == ["w.yml:j is ungated without a reason"]


def test_a_new_job_that_reads_only_a_cloudflare_dns_token_is_reported():
    job = {"steps": [{"run": "x", "env": {"K": "${{ secrets.CF_API_TOKEN }}"}}]}
    errors = contract_errors(_workflow(job), {}, {})
    assert len(errors) == 1 and "['CF_API_TOKEN']" in errors[0]


def test_a_job_with_workflow_level_contents_write_is_reported():
    workflows = {
        "w.yml": {"permissions": {"contents": "write"}, "jobs": {"j": {"steps": []}}}
    }
    errors = contract_errors(workflows, {}, {})
    assert len(errors) == 1 and "permission contents: write" in errors[0]


def test_a_job_level_permission_overrides_the_workflow_level_write():
    workflows = {
        "w.yml": {
            "permissions": {"contents": "write"},
            "jobs": {"j": {"permissions": {"contents": "read"}, "steps": []}},
        }
    }
    assert contract_errors(workflows, {}, {}) == []


def test_a_job_with_write_all_permissions_is_reported():
    workflows = {"w.yml": {"jobs": {"j": {"permissions": "write-all", "steps": []}}}}
    assert len(contract_errors(workflows, {}, {})) == 1


def test_an_ungated_job_must_not_hold_contents_write():
    job = {**_READS_SECRET, "permissions": {"contents": "write"}}
    errors = contract_errors(_workflow(job), {}, {_KEY: "scheduled read-only audit"})
    assert errors == ["w.yml:j is ungated and holds permission contents: write"]
