"""Contract: a job that holds a production credential declares its environment (#1125, #1035).

A GitHub environment with a required reviewer is the only mechanism that stops a
production job before it runs. A job that reads a production credential must declare
`environment:`, or the contract must name it as not production, with a reason.

The tables and the check live in `libs/gate/production_contract.py`, because the merge
gate reads them too (#1138).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from libs.gate import production_contract
from libs.gate.production_contract import (
    GATED_JOBS,
    NON_PRODUCTION_SECRETS,
    UNGATED_JOBS,
    _production_credentials,
    contract_errors,
    workflow_contract_failures,
)

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = ROOT / ".github" / "workflows"


def _load_workflows(directory: Path) -> dict[str, dict]:
    workflows = {}
    for path in sorted(directory.glob("*.y*ml")):
        workflows[path.name] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return workflows


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
    assert errors == [
        "w.yml:j environment condition must be exactly \"inputs.type == 'prod'\", "
        "not 'inputs.other'"
    ]


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


# --- the gate's entry point: YAML texts of the pull request head (#1138) -------------


@pytest.fixture
def _no_tables(monkeypatch):
    """Empty tables, so a one-file tree can satisfy the contract."""
    monkeypatch.setattr(production_contract, "GATED_JOBS", {})
    monkeypatch.setattr(production_contract, "UNGATED_JOBS", {})


_SECRET_JOB = (
    "on: push\njobs:\n  j:\n    steps:\n"
    "      - run: x\n        env:\n          K: ${{ secrets.DOKPLOY_API_KEY }}\n"
)


@pytest.mark.usefixtures("_no_tables")
def test_texts_without_production_credentials_satisfy_the_contract():
    text = "on: push\njobs:\n  j:\n    steps:\n      - run: echo ${{ secrets.GITHUB_TOKEN }}\n"
    assert workflow_contract_failures({"w.yml": text}) == []


@pytest.mark.usefixtures("_no_tables")
def test_texts_report_the_same_errors_as_parsed_workflows():
    texts = {"w.yml": _SECRET_JOB}
    parsed = {"w.yml": yaml.safe_load(_SECRET_JOB)}
    assert workflow_contract_failures(texts) == contract_errors(parsed, {}, {})
    assert len(workflow_contract_failures(texts)) == 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("on: [", "w.yml does not parse as YAML"),
        ("- a\n- b\n", "w.yml is not a YAML mapping"),
        ("jobs: [a, b]\n", "w.yml has a jobs section that is not a mapping of jobs"),
        (
            "jobs:\n  a: text\n",
            "w.yml has a jobs section that is not a mapping of jobs",
        ),
    ],
    ids=["broken-yaml", "a-list", "jobs-a-list", "job-not-a-mapping"],
)
def test_an_unreadable_file_is_the_only_failure(text, expected):
    # The real tables are in force: the contract must not run on a partial tree and
    # blame every job of the unreadable file as missing.
    assert workflow_contract_failures({"w.yml": text}) == [expected]


@pytest.mark.usefixtures("_no_tables")
def test_a_production_secret_read_outside_a_job_is_reported():
    text = (
        "on: push\nenv:\n  K: ${{ secrets.DOKPLOY_API_KEY }}\n"
        "jobs:\n  j:\n    steps:\n      - run: x\n"
    )
    assert workflow_contract_failures({"w.yml": text}) == [
        "w.yml reads production credentials ['DOKPLOY_API_KEY'] outside a job"
    ]


@pytest.mark.usefixtures("_no_tables")
@pytest.mark.parametrize(
    "expression",
    [
        "${{ toJSON(secrets) }}",
        "${{ secrets['DOKPLOY_API_KEY'] }}",
        "${{ secrets[format('{0}', 'X')] }}",
        "${{ secrets.dokploy_api_key }}",
        "${{ SECRETS.DOKPLOY_API_KEY }}",
        "${{ secrets.* }}",
        "${{\n  secrets\n}}",
    ],
)
def test_a_secret_read_by_a_name_that_is_not_literal_is_reported(expression):
    block = expression.replace("\n", "\n          ")
    text = (
        f"on: push\njobs:\n  j:\n    steps:\n      - run: |\n          echo {block}\n"
    )
    failures = workflow_contract_failures({"w.yml": text})
    # A name in another case also reads DOKPLOY_API_KEY, so the default-deny scan adds
    # its own line for those two forms. The line asserted here comes only from the
    # expression scan.
    assert failures[0] == "w.yml reads a secret by a name that is not literal"
    assert all("holds production credentials" in f for f in failures[1:]), failures


@pytest.mark.usefixtures("_no_tables")
@pytest.mark.parametrize(
    "line",
    [
        "# read secrets from the vault, not secrets[0]",
        "if: ${{ inputs.task == 'secrets-reconcile' }}",
        "group: ${{ format('infra2-secrets-{0}', github.sha) }}",
        "token: ${{ secrets.INFRA2_REPORTS_FEISHU_APP_ID || github.token }}",
    ],
)
def test_prose_and_string_literals_are_not_secret_reads(line):
    text = f"on: push\njobs:\n  j:\n    {line}\n    steps:\n      - run: x\n"
    assert workflow_contract_failures({"w.yml": text}) == []


@pytest.mark.usefixtures("_no_tables")
def test_passing_every_secret_to_a_called_workflow_is_reported():
    called = "o/r/.github/workflows/" + "x.yml@v1"
    text = f"on: push\njobs:\n  call:\n    uses: {called}\n    secrets: inherit\n"
    assert workflow_contract_failures({"w.yml": text}) == [
        "w.yml:call passes every secret to a called workflow"
    ]


def test_no_workflow_file_is_held_with_the_owner_by_decision_of_2026_10_08():
    """The lock covers every workflow file (#1138). The credentials that ungated jobs
    read are the open risk; scoping them is tracked in #1147."""
    assert production_contract.OWNER_HELD_WORKFLOWS == frozenset()


# --- #1138 audit: the condition is exact, the secret list is default deny -----------


def _conditional(condition: str) -> dict[str, dict]:
    return _workflow(
        {
            **_READS_SECRET,
            "environment": f"${{{{ {condition} && 'production' || 'staging' }}}}",
        }
    )


@pytest.mark.parametrize(
    ("selector", "condition"),
    [
        ("inputs.type == 'prod'", "inputs.type == 'prod' && false"),
        ("inputs.promote_prod", "!inputs.promote_prod"),
        ("inputs.type == 'prod'", "inputs.type == 'prod' || inputs.other"),
        ("inputs.type == 'prod'", "(inputs.type == 'prod')"),
        ("inputs.type == 'prod'", "inputs.type == 'prod' && inputs.dry_run"),
        ("inputs.type == 'prod'", "!(inputs.type == 'prod')"),
    ],
    ids=[
        "and-false",
        "negation",
        "or-extra",
        "parentheses",
        "and-extra",
        "negated-group",
    ],
)
def test_a_condition_that_only_contains_the_selector_is_reported(selector, condition):
    errors = contract_errors(_conditional(condition), {_KEY: selector}, {})
    assert errors == [
        f"w.yml:j environment condition must be exactly {selector!r}, not {condition!r}"
    ]


def test_an_extra_branch_after_staging_is_not_the_gated_form():
    job = {
        **_READS_SECRET,
        "environment": "${{ inputs.type == 'prod' && 'production' || 'staging' || 'x' }}",
    }
    errors = contract_errors(_workflow(job), {_KEY: "inputs.type == 'prod'"}, {})
    assert len(errors) == 1 and "environment must be" in errors[0]


def test_white_space_in_the_condition_does_not_matter():
    workflows = _conditional("inputs.type   ==\t 'prod'")
    assert contract_errors(workflows, {_KEY: "inputs.type == 'prod'"}, {}) == []


@pytest.mark.parametrize("selector", ["", "   "])
def test_an_empty_selector_matches_no_condition(selector):
    for environment in (
        "${{   && 'production' || 'staging' }}",
        "${{ inputs.type == 'prod' && 'production' || 'staging' }}",
    ):
        job = {**_READS_SECRET, "environment": environment}
        assert contract_errors(_workflow(job), {_KEY: selector}, {}), environment


def test_every_selector_in_the_table_is_a_non_empty_string_or_none():
    for key, selector in GATED_JOBS.items():
        assert selector is None or (
            isinstance(selector, str) and selector.strip() == selector and selector
        ), key


def test_the_table_holds_the_full_conditions_of_the_real_workflows():
    assert {k: v for k, v in GATED_JOBS.items() if v is not None} == {
        ("deploy.yml", "deploy"): "inputs.type == 'prod'",
        ("reconcile-iac-inputs.yml", "reconcile"): "inputs.promote_prod",
        (
            "app-deploy-request.yml",
            "deploy",
        ): "github.event.client_payload.deploy_type == 'prod'",
    }


def test_the_real_workflow_tree_satisfies_the_gate_entry_point():
    texts = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(WORKFLOWS_DIR.glob("*.y*ml"))
    }
    assert len(texts) >= 10, sorted(texts)
    assert workflow_contract_failures(texts) == []


def test_the_benign_secret_list_is_pinned():
    assert NON_PRODUCTION_SECRETS == (
        "GITHUB_TOKEN",
        "INFRA2_OUT_OF_BAND_ALERT_DELIVERY_MODE",
        "INFRA2_OUT_OF_BAND_FEISHU_API_BASE",
        "INFRA2_OUT_OF_BAND_FEISHU_APP_ID",
        "INFRA2_OUT_OF_BAND_FEISHU_APP_SECRET",
        "INFRA2_OUT_OF_BAND_FEISHU_CHAT_ID",
        "INFRA2_OUT_OF_BAND_FEISHU_WEBHOOK_URL",
        "INFRA2_REPORTS_FEISHU_APP_ID",
        "INFRA2_REPORTS_FEISHU_APP_SECRET",
        "INFRA2_REPORTS_FEISHU_CHAT_ID",
        "ZAI_CODING_CN_API_KEY",
    )


def _reads(name: str) -> dict[str, dict]:
    return _workflow(
        {"steps": [{"run": "x", "env": {"K": f"${{{{ secrets.{name} }}}}"}}]}
    )


@pytest.mark.parametrize("name", NON_PRODUCTION_SECRETS)
def test_a_job_that_reads_only_a_benign_secret_needs_no_entry(name):
    assert contract_errors(_reads(name), {}, {}) == []


@pytest.mark.parametrize(
    "name",
    [
        "A_SECRET_ADDED_TOMORROW",
        "INFRA2_HOOKS_READ_TOKEN",
        "INFRA2_WATCHDOG_SSH_USER",
        "INFRA2_WATCHDOG_WORKER_STATUS_TOKEN",
        "PREVIEW_LEAK_GH_TOKEN",
        "CF_API_TOKEN",
        "CF_ZONE_ID",
        "INFRA2_OUT_OF_BAND_NEW_TOKEN",
    ],
)
def test_any_other_secret_makes_a_new_job_credentialed(name):
    assert contract_errors(_reads(name), {}, {}) == [
        f"w.yml:j holds production credentials ['{name}'] and is in neither "
        "GATED_JOBS nor UNGATED_JOBS"
    ]


def test_a_secret_name_in_another_case_is_the_same_secret():
    assert len(contract_errors(_reads("dokploy_api_key"), {}, {})) == 1
    assert contract_errors(_reads("github_token"), {}, {}) == []
