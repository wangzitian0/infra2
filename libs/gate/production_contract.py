"""Contract: a job that holds a production credential declares its environment (#1125, #1035).

A GitHub environment with a required reviewer is the only mechanism that stops a
production job before it runs. A job that reads a production credential must declare
`environment:`, or the contract must name it as not production, with a reason.

The merge gate reads this contract (#1138). It applies the tables of the gate checkout
to the workflows of the pull request head.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

import yaml

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


def exposed_workflows() -> frozenset[str]:
    """Workflow file names with a job that can hold a production credential outside
    the `production` environment: an ungated job, or a gated job with a condition.

    The environment reviewer does not stop such a job. A change to its body can reach
    production, so the merge gate keeps these files with the owner (#1138).
    """
    ungated = {workflow for workflow, _ in UNGATED_JOBS}
    conditional = {
        workflow
        for (workflow, _), selector in GATED_JOBS.items()
        if selector is not None
    }
    return frozenset(ungated | conditional)


# A `secrets` reference in an expression that is not the literal `secrets.NAME` form.
# The name scan of `_secrets_read` cannot see which secret such a reference reads.
# String literals ('...', with '' as the escape) are removed first: they are not reads.
_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
_OPAQUE_SECRETS = re.compile(r"(?<![\w.-])(?i:secrets)\b(?!\.[A-Z0-9_]+\b)")
_ANY_SECRETS = re.compile(r"(?<![\w.-])(?i:secrets)\b")


def _opaque_secret_reads(text: str) -> bool:
    for expression in _EXPRESSION.finditer(text):
        body = _STRING_LITERAL.sub("''", expression.group(1))
        if _OPAQUE_SECRETS.search(body):
            return True
        if any(match.group(0) != "secrets" for match in _ANY_SECRETS.finditer(body)):
            return True
    return False


def workflow_contract_failures(texts: Mapping[str, str]) -> list[str]:
    """Contract failures for workflow file name -> YAML text. Empty means satisfied.

    A file that does not parse, or that has no mapping of jobs, is a failure. The
    contract runs only when every file parses, so a missing file cannot read as a
    missing job. Three forms that the name scan cannot see are also failures: a
    production secret read outside a job, a secret read by a computed name, and
    `secrets: inherit`.
    """
    failures: list[str] = []
    workflows: dict[str, dict] = {}
    for name, text in sorted(texts.items()):
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError:
            failures.append(f"{name} does not parse as YAML")
            continue
        document = {} if document is None else document
        if not isinstance(document, dict):
            failures.append(f"{name} is not a YAML mapping")
            continue
        jobs = document.get("jobs") or {}
        if not isinstance(jobs, dict) or not all(
            isinstance(job, dict) for job in jobs.values()
        ):
            failures.append(f"{name} has a jobs section that is not a mapping of jobs")
            continue
        outside = _secrets_read({k: v for k, v in document.items() if k != "jobs"})
        if outside:
            failures.append(
                f"{name} reads production credentials {sorted(outside)} outside a job"
            )
        if _opaque_secret_reads(text):
            failures.append(f"{name} reads a secret by a name that is not literal")
        for job_name, job in jobs.items():
            if job.get("secrets") == "inherit":
                failures.append(
                    f"{name}:{job_name} passes every secret to a called workflow"
                )
        workflows[name] = document
    if len(workflows) < len(texts):
        return failures
    return failures + contract_errors(workflows, GATED_JOBS, UNGATED_JOBS)
