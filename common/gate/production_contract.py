"""Contract: a job that holds a production credential declares its environment (#1125, #1035).

A GitHub environment with a required reviewer is the only mechanism that stops a
production job before it runs. A job that reads a production credential must declare
`environment:`, or the contract must name it as not production, with a reason.

The merge gate reads this contract (#1138). It applies the tables of the gate checkout
to the workflows of the pull request head.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Iterator, Mapping

import yaml

# Default deny (#1138): a job that reads any secret not named here holds a production
# credential. These secrets reach only alert and report channels, a model API, or the
# job's own GitHub token.
NON_PRODUCTION_SECRETS = (
    "GITHUB_TOKEN",
    "ZAI_CODING_CN_API_KEY",
)

# Jobs that can change production. Value: the full production condition, or None when
# the job always runs against production. The environment condition must equal it after
# whitespace normalization (#1138): a condition that only contains it can be negated.
GATED_JOBS: dict[tuple[str, str], str | None] = {
    ("deploy.yml", "deploy"): "inputs.type == 'prod'",
    ("deploy.yml", "bootstrap"): None,
    ("reconcile-iac-inputs.yml", "reconcile"): "inputs.promote_prod",
    (
        "app-deploy-request.yml",
        "deploy",
    ): "github.event.client_payload.deploy_type == 'prod'",
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
    (
        "ops-checks.yml",
        "host-hygiene-schedule",
    ): "scheduled idempotent host-hygiene schedule ensure (owner approved 2026-10-20, #1127)",
    (
        "ops-checks.yml",
        "facet-reconcile",
    ): "scheduled read-only drift report (#1127)",
}

# Used with fullmatch: a block scalar adds a final newline, and that form fails (#1138).
_GATED_EXPRESSION = re.compile(
    r"\$\{\{\s*(?P<condition>.+?)\s*&&\s*'production'\s*\|\|\s*'staging'\s*\}\}"
)

# The contract reads parsed values only, never raw file text: a YAML escape such as
# `s\x65crets` changes the raw text but not the value that GitHub reads (#1138).
# String literals ('...', with '' as the escape) are removed before each scan.
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
_SECRET_NAME = re.compile(r"(?<![\w.-])(?i:secrets)\.([A-Za-z0-9_]+)")
# A `secrets` reference that is not the literal `secrets.NAME` form hides the name.
_OPAQUE_SECRETS = re.compile(r"(?<![\w.-])(?i:secrets)\b(?!\.[A-Z0-9_]+\b)")
_ANY_SECRETS = re.compile(r"(?<![\w.-])(?i:secrets)\b")


def _expression_bodies(text: str) -> list[str]:
    """The body of each `${{ ... }}` in `text`, read two ways. One way ends at the
    first `}}` outside a string literal; the other ends at the first `}}`. Both are
    scanned, so the result fails closed whichever way GitHub reads it.

    Both reads move forward only, so the time is linear in the text (#1138): an
    expression without an end runs to the end of the text, and the scan stops."""
    bodies: list[str] = []
    position = 0
    while (start := text.find("${{", position)) != -1:
        end = text.find("}}", start + 3)
        if end == -1:
            bodies.append(text[start + 3 :])
            break
        bodies.append(text[start + 3 : end])
        position = end + 2
    start = text.find("${{")
    while start != -1:
        index, quoted = start + 3, False
        while index < len(text):
            if text[index] == "'":
                if quoted and text.startswith("''", index):
                    index += 2
                    continue
                quoted = not quoted
            elif not quoted and text.startswith("}}", index):
                break
            index += 1
        bodies.append(text[start + 3 : index])
        start = text.find("${{", index + 2)
    return [_STRING_LITERAL.sub("''", body) for body in bodies]


def _expressions(node: object, seen: set[int] | None = None) -> Iterator[str]:
    """Each expression body in a parsed workflow, from every string key and value at
    any depth. The value of an `if:` key is an expression even without `${{ }}`.
    A mapping or list that a YAML alias shares is read once."""
    seen = set() if seen is None else seen
    if isinstance(node, (dict, list)):
        if id(node) in seen:
            return
        seen.add(id(node))
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _expressions(key, seen)
            if key == "if" and isinstance(value, str) and "${{" not in value:
                yield _STRING_LITERAL.sub("''", value)
            else:
                yield from _expressions(value, seen)
    elif isinstance(node, list):
        for item in node:
            yield from _expressions(item, seen)
    elif isinstance(node, str):
        yield from _expression_bodies(node)


class _UnreadableYaml(Exception):
    """A YAML shape that the contract cannot read as one tree."""


_BINARY_TAG = "tag:yaml.org,2002:binary"


def _scalar_text(node: yaml.ScalarNode) -> str:
    """The source text of a scalar. A `!!binary` scalar adds its decoded text."""
    text = str(node.value)
    if node.tag == _BINARY_TAG:
        try:
            text += "\n" + base64.b64decode(text, validate=False).decode(
                "utf-8", "replace"
            )
        except (ValueError, binascii.Error):
            pass
    return text


def _plain(node: yaml.Node | None, active: set[int], done: dict[int, object]) -> object:
    """The composed YAML node as plain data. Every scalar is its source text and every
    tag is ignored, so `!!null "${{ x }}"`, `!!set` and `!!omap` cannot hide an
    expression (#1138 round 3). A recursive alias is unreadable."""
    if node is None:
        return {}
    if id(node) in done:
        return done[id(node)]
    if isinstance(node, yaml.ScalarNode):
        result: object = _scalar_text(node)
    elif id(node) in active:
        raise _UnreadableYaml("a recursive YAML alias")
    else:
        active.add(id(node))
        if isinstance(node, yaml.SequenceNode):
            result = [_plain(item, active, done) for item in node.value]
        else:
            result = {}
            for key_node, value_node in node.value:
                key = _plain(key_node, active, done)
                if not isinstance(key, str):
                    key = " ".join(_expressions(key)) or repr(key)
                result[key] = _plain(value_node, active, done)
        active.discard(id(node))
    done[id(node)] = result
    return result


def _secrets_read(node: object) -> set[str]:
    """Production secrets that `node` reads in its expressions: every named secret not
    in the benign list. Secret names are not case sensitive, so they compare in upper
    case."""
    return {
        match.group(1).upper()
        for body in _expressions(node)
        for match in _SECRET_NAME.finditer(body)
        if match.group(1).upper() not in NON_PRODUCTION_SECRETS
    }


def _opaque_secret_reads(node: object) -> bool:
    """True when an expression of `node` reads `secrets` other than as `secrets.NAME`."""
    for body in _expressions(node):
        if _OPAQUE_SECRETS.search(body):
            return True
        if any(match.group(0) != "secrets" for match in _ANY_SECRETS.finditer(body)):
            return True
    return False


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
    match = _GATED_EXPRESSION.fullmatch(environment)
    if match is None:
        return f"{label} environment must be '${{{{ <condition> && 'production' || 'staging' }}}}'"
    condition = _normalized(match.group("condition"))
    if not selector.strip() or condition != _normalized(selector):
        return f"{label} environment condition must be exactly {selector!r}, not {condition!r}"
    return None


def _normalized(expression: str) -> str:
    """The expression with each run of white space replaced by one space."""
    return " ".join(expression.split())


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


# Unclassified scheduled jobs in ops-checks.yml that require owner approval on edit (#1340).
UNCLASSIFIED_SCHEDULED_JOBS: tuple[str, ...] = (
    "host-hygiene-schedule",
    "facet-reconcile",
)


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
            document = _plain(yaml.compose(text), set(), {})
        except _UnreadableYaml as exc:
            failures.append(f"{name} has {exc}")
            continue
        except (yaml.YAMLError, RecursionError):
            failures.append(f"{name} does not parse as YAML")
            continue
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
        if _opaque_secret_reads(document):
            failures.append(f"{name} reads a secret by a name that is not literal")
        for job_name, job in jobs.items():
            if job.get("secrets") == "inherit":
                failures.append(
                    f"{name}:{job_name} passes every secret to a called workflow"
                )
        workflows[name] = document
    if len(workflows) < len(texts):
        return failures
    if GATED_JOBS or UNGATED_JOBS:
        from common.gate.inventory import _required_check_workflows
        from common.gate.types import (
            WORKFLOW_PREFIX,
            _get_workflow_dir,
            _normalized_path,
        )

        required_workflows = _required_check_workflows()
        if required_workflows is None:
            failures.append(
                "cannot read a blocking gate with a workflow from docs/ssot/ci-gate-inventory.yaml"
            )
        else:
            present = {
                _normalized_path(f"{WORKFLOW_PREFIX}{name}") for name in workflows
            }
            for req in sorted(required_workflows):
                if req not in present:
                    failures.append(
                        f"required-check workflow {req} is missing from .github/workflows"
                    )
        if "ops-checks.yml" in workflows:
            base_ops_path = _get_workflow_dir() / "ops-checks.yml"
            try:
                base_ops_doc = _plain(
                    yaml.compose(base_ops_path.read_text(encoding="utf-8")),
                    set(),
                    {},
                )
            except (OSError, _UnreadableYaml, yaml.YAMLError, RecursionError):
                base_ops_doc = None
            if isinstance(base_ops_doc, dict):
                base_ops_jobs = base_ops_doc.get("jobs") or {}
                head_ops_jobs = workflows["ops-checks.yml"].get("jobs") or {}
                if isinstance(base_ops_jobs, dict) and isinstance(head_ops_jobs, dict):
                    for job_name in UNCLASSIFIED_SCHEDULED_JOBS:
                        if head_ops_jobs.get(job_name) != base_ops_jobs.get(job_name):
                            failures.append(
                                f"ops-checks.yml:{job_name} edits an unclassified scheduled production job"
                            )
    return failures + contract_errors(workflows, GATED_JOBS, UNGATED_JOBS)
