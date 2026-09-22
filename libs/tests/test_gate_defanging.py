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


# -- An exemption exempts one thing (#788 review) ------------------------------------

JOB_LEVEL_PLUS_EXEMPTED_STEP = """
jobs:
  gate:
    continue-on-error: true
    runs-on: ubuntu-latest
    steps:
      - name: Run ruff check
        run: ruff check .
      - name: Upload coverage
        # gate-exempt: reports a number, does not check one
        continue-on-error: true
        uses: coverallsapp/github-action@v2
"""

EXEMPTED_JOB = """
jobs:
  gate:
    # gate-exempt: advisory only, declared blocks_merge:false
    continue-on-error: true
    runs-on: ubuntu-latest
    steps:
      - name: Run ruff check
        run: ruff check .
"""


def test_an_exempted_step_does_not_silence_its_job() -> None:
    """The bypass this audit would otherwise have rebuilt inside itself.

    The job is `continue-on-error: true` AND an unrelated coverage step carries a
    `# gate-exempt:`. Reading the exemptions as one workflow-wide flag (`not exempt`)
    made the second fact hide the first: add one exempted step anywhere in the file
    and a required job could be defanged silently -- exactly the drift this audit
    exists to detect.
    """
    found = ci_spec.defanged_steps(
        _wf(JOB_LEVEL_PLUS_EXEMPTED_STEP), "gate", JOB_LEVEL_PLUS_EXEMPTED_STEP
    )
    assert found == ["<job>"], found


def test_a_job_can_be_exempted_by_its_own_comment() -> None:
    """The counterpart: the exemption keyed to the job itself does apply to it,
    so the mechanism still has a legitimate job-level use."""
    assert ci_spec.defanged_steps(_wf(EXEMPTED_JOB), "gate", EXEMPTED_JOB) == []


def test_one_jobs_exemption_does_not_carry_to_another() -> None:
    """Keying is per declaration, not 'the last comment seen wins the file'."""
    two_jobs = (
        EXEMPTED_JOB
        + """  other:
    continue-on-error: true
    runs-on: ubuntu-latest
    steps:
      - name: Run tests
        run: pytest
"""
    )
    assert ci_spec.defanged_steps(_wf(two_jobs), "gate", two_jobs) == []
    assert ci_spec.defanged_steps(_wf(two_jobs), "other", two_jobs) == ["<job>"]


# -- load_workflow's docstring promise (#788 review) ----------------------------------


@pytest.mark.parametrize("body", ["", "null\n", "- a\n- b\n", "just a string\n"])
def test_valid_yaml_that_is_not_a_mapping_reads_as_empty(tmp_path, body) -> None:
    """`yaml.safe_load` happily returns None/list/str, which sails past the
    YAMLError handler and breaks every `.get()` downstream."""
    p = tmp_path / "wf.yml"
    p.write_text(body, encoding="utf-8")
    assert ci_spec.load_workflow(p) == {}


def test_an_unreadable_file_reads_as_empty(tmp_path) -> None:
    """The docstring promised {} for 'absent or unreadable', but only malformed
    YAML was caught -- a permission error or invalid UTF-8 raised through it."""
    bad_utf8 = tmp_path / "bad.yml"
    bad_utf8.write_bytes(b"jobs:\n  gate:\n    name: \xff\xfe\n")
    assert ci_spec.load_workflow(bad_utf8) == {}

    no_read = tmp_path / "locked.yml"
    no_read.write_text("jobs: {}\n", encoding="utf-8")
    no_read.chmod(0o000)
    try:
        # chmod does not stop root, and permission semantics differ by platform
        # (#788 review). Assert the OSError path only where the chmod actually
        # took effect; otherwise this fails for a reason that is not the code's.
        try:
            no_read.read_bytes()
        except OSError:
            assert ci_spec.load_workflow(no_read) == {}
        else:
            pytest.skip("this environment can read a 0o000 file")
    finally:
        no_read.chmod(0o644)


# -- One read, and an exemption that stays where it was written (#788 review 2) ------


def test_read_workflow_returns_parse_and_source_together(tmp_path) -> None:
    """Two reads meant two failure paths and one handler; callers take both here."""
    wf = tmp_path / "wf.yml"
    wf.write_text(JOB_LEVEL, encoding="utf-8")
    parsed, source = ci_spec.read_workflow(wf)
    assert parsed["jobs"]["gate"]["continue-on-error"] is True
    assert source == JOB_LEVEL


def test_read_workflow_of_an_unreadable_file_yields_no_source(tmp_path) -> None:
    """The second read was the unhandled one: a caller that got {} from
    load_workflow still crashed fetching the comments."""
    bad = tmp_path / "bad.yml"
    bad.write_bytes(b"jobs:\n  gate:\n    name: \xff\xfe\n")
    assert ci_spec.read_workflow(bad) == ({}, "")
    assert ci_spec.read_workflow(tmp_path / "absent.yml") == ({}, "")


def test_read_workflow_keeps_the_text_when_only_the_yaml_is_broken(tmp_path) -> None:
    wf = tmp_path / "wf.yml"
    wf.write_text("jobs: [\n", encoding="utf-8")
    parsed, source = ci_spec.read_workflow(wf)
    assert parsed == {} and source == "jobs: [\n"


UNNAMED_EXEMPT_STEP = """
jobs:
  gate:
    continue-on-error: true
    runs-on: ubuntu-latest
    steps:
      - # gate-exempt: nothing should be exempted by this
        run: ./flaky.sh
        continue-on-error: true
"""


def test_a_comment_on_an_unnamed_step_exempts_nothing() -> None:
    """The bypass one level down from the last one.

    `current` used to survive into the next declaration, so a bare
    `- # gate-exempt:` still pointed at the enclosing job and silenced the job's
    own `continue-on-error`. The docstring already promised it exempted nothing.
    """
    found = ci_spec.defanged_steps(
        _wf(UNNAMED_EXEMPT_STEP), "gate", UNNAMED_EXEMPT_STEP
    )
    assert "<job>" in found, found
    assert any(f.startswith("step #") for f in found), found


def test_an_exemption_can_be_keyed_to_a_step_named_only_by_uses() -> None:
    """`defanged_steps` reports such a step by its `uses`, so that is the name an
    exemption has to be written against for a reader to match them up."""
    wf = """
jobs:
  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: coverallsapp/github-action@v2
        # gate-exempt: reports a number, does not check one
        continue-on-error: true
"""
    assert ci_spec.defanged_steps(_wf(wf), "gate", wf) == []


@pytest.mark.parametrize(
    "node", ["jobs:\n  gate: notamapping\n", "jobs:\n  gate:\n    - a\n"]
)
def test_a_job_node_that_is_not_a_mapping_is_not_a_crash(node) -> None:
    assert ci_spec.defanged_steps(_wf(node), "gate", node) == []


def test_steps_that_are_not_a_list_of_mappings_are_not_a_crash() -> None:
    wf = "jobs:\n  gate:\n    continue-on-error: true\n    steps: oops\n"
    assert ci_spec.defanged_steps(_wf(wf), "gate", wf) == ["<job>"]
    wf2 = "jobs:\n  gate:\n    steps:\n      - just a string\n"
    assert ci_spec.defanged_steps(_wf(wf2), "gate", wf2) == []
