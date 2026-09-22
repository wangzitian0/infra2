"""A required check can be turned off without being removed, and three auditors missed it.

`continue-on-error` leaves a job's name, `if:` and `needs:` byte-identical
while its check becomes unconditionally green. `ci-gate-inventory.yaml` knows
this matters — `infra_ci.vault_policy` is declared `blocks_merge: false` for
exactly that reason — but the detection read only `job.get("continue-on-error")`.
One level down was invisible: a `continue-on-error` on the step that runs
`ruff`, or the tests, defangs the gate and `ci_gate_audit`, `ci_gate_lint` and
`ci_gate_ruleset_audit` all passed it. Measured before this change.

It stayed missed because the same question was answered in three files and
deepened in none — six `yaml.safe_load` calls over the same workflows. The
reading now lives in `tools.ci_spec`, so there is one place to fix.

Not every `continue-on-error` is wrong: uploading coverage to a third party
should not fail a test job. Telling those apart by what a step "looks like" is
an open set, so this uses the convention the repository already runs on
(`# schedule-signal-exempt:`, `# alert-delivery-exempt:`) — the exemption is
written next to the thing it exempts, and reviewed.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from tools import ci_spec

ROOT = pathlib.Path(__file__).resolve().parents[2]
INFRA_CI = ROOT / ".github" / "workflows" / "infra-ci.yml"


def _wf(text: str) -> dict:
    return yaml.safe_load(text)


STEP_LEVEL = """
jobs:
  gate:
    steps:
      - name: Run ruff check
        continue-on-error: true
        run: ruff check .
"""

JOB_LEVEL = """
jobs:
  gate:
    continue-on-error: true
    steps:
      - name: Run ruff check
        run: ruff check .
"""

EXEMPTED = """
jobs:
  gate:
    steps:
      - name: Upload coverage
        # gate-exempt: reports a number, does not check one
        continue-on-error: true
        uses: coverallsapp/github-action@v2
"""


def test_a_step_level_continue_on_error_is_found() -> None:
    """The case that was invisible, and the reason this file exists."""
    assert ci_spec.defanged_steps(_wf(STEP_LEVEL), "gate", STEP_LEVEL) == [
        "Run ruff check"
    ]


def test_job_level_is_still_found() -> None:
    """Deepening the check must not lose the level it already had."""
    assert ci_spec.defanged_steps(_wf(JOB_LEVEL), "gate", JOB_LEVEL) == ["<job>"]


def test_a_declared_exemption_is_not_a_finding() -> None:
    """Otherwise the check is noise, and a noisy gate gets switched off."""
    assert ci_spec.defanged_steps(_wf(EXEMPTED), "gate", EXEMPTED) == []


def test_an_undeclared_exemption_is_a_finding() -> None:
    """The control for the test above: the comment is doing the work, not the
    step's name or what it uses."""
    without = EXEMPTED.replace(
        "        # gate-exempt: reports a number, does not check one\n", ""
    )
    assert ci_spec.defanged_steps(_wf(without), "gate", without) == ["Upload coverage"]


def test_without_the_source_every_one_is_reported() -> None:
    """Exemptions live in comments, which YAML parsing discards.

    A caller that cannot supply the text gets the unfiltered list rather than a
    quietly shorter one — wrong in the direction that shows up.
    """
    assert ci_spec.defanged_steps(_wf(EXEMPTED), "gate") == ["Upload coverage"]


def test_a_comment_on_an_unnamed_step_exempts_nothing() -> None:
    """An exemption nobody can point at is not one — the report names steps."""
    unnamed = """
jobs:
  gate:
    steps:
      - # gate-exempt: no name to attach this to
        continue-on-error: true
        run: echo hi
"""
    assert ci_spec.defanged_steps(_wf(unnamed), "gate", unnamed)


@pytest.mark.parametrize(
    "job", ("lint-python", "test-deployer-logic", "validate-compose")
)
def test_the_real_required_jobs_carry_no_undeclared_exemption(job: str) -> None:
    """The live assertion. A new `continue-on-error` on a required job has to
    explain itself before this goes green again."""
    source = INFRA_CI.read_text(encoding="utf-8")
    found = ci_spec.defanged_steps(ci_spec.load_workflow(INFRA_CI), job, source)
    assert not found, (
        f"{job} has step(s) whose failure cannot fail it and which say no why: "
        f"{found}. Add `# gate-exempt: <reason>` if that is deliberate."
    )


def test_a_missing_workflow_reports_nothing_rather_than_raising() -> None:
    """An auditor's job is to report; a deleted workflow is a finding for the
    caller to phrase, not a traceback."""
    # Built from segments: spelling it whole would make
    # test_workflow_reference_contract read it as a reference to a
    # workflow that ought to exist.
    absent = ROOT / ".github" / "workflows" / "does-not-exist.yml"
    assert ci_spec.load_workflow(absent) == {}
