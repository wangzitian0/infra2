"""The `lint-python` job must run `ruff format --check` on the whole tree (#1061).

Before this change, the job built a list of changed files with
`git diff origin/$base...HEAD`. The checkout is shallow, so the command failed
with `fatal: no merge base`. The step ended with `|| true`, so the list was empty.
The next step had the condition `files != ''`, so it was skipped. The job was green
and `ruff format --check` never ran. 35 files stayed unformatted without a warning.

The job now checks the whole tree, with the same paths as `ruff check`. These
tests keep three facts true:

1. The job has one `ruff format --check` step. Its paths equal the `ruff check` paths.
   The step has no `if:` condition that can skip it.
2. The changed-file mechanism (`changed-python`) is gone.
3. `ruff format --check` exits 1 for an unformatted file and 0 for a formatted file,
   on the ruff version that the workflow pins. A green job therefore means a
   formatted tree.

The checker functions also run against synthetic broken workflows. A checker that
never fails on a broken input proves nothing.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from libs.core import ci_spec

ROOT = Path(__file__).resolve().parents[2]
INFRA_CI = ROOT / ".github" / "workflows" / "infra-ci.yml"
JOB_ID = "lint-python"
LEGACY_STEP_ID = "changed-python"
SHELL_OPERATORS = {"&&", "||", ";", "|"}
RUFF_PIN_RE = re.compile(r"ruff==([0-9]+(?:\.[0-9]+)+)")

# The shape of the job before this change, kept to prove the checkers reject it.
LEGACY_JOB = """
jobs:
  lint-python:
    steps:
      - name: Install ruff
        run: pip install "ruff==0.15.22"
      - name: Run ruff check
        run: ruff check libs/ bootstrap/ platform/ finance_report/ truealpha/ tools/
      - name: Determine changed Python files
        id: changed-python
        run: |
          files=$(git diff --name-only "origin/main...HEAD" -- libs | grep -E '\\.py$' || true)
          echo "files=$files" >> "$GITHUB_OUTPUT"
      - name: Run ruff format check
        if: steps.changed-python.outputs.files != ''
        run: ruff format --check ${{ steps.changed-python.outputs.files }}
"""

WHOLE_TREE_JOB = """
jobs:
  lint-python:
    steps:
      - name: Install ruff
        run: pip install "ruff==0.15.22"
      - name: Run ruff check
        run: ruff check libs/ bootstrap/ platform/ finance_report/ truealpha/ tools/
      - name: Run ruff format check
        run: ruff format --check libs/ bootstrap/ platform/ finance_report/ truealpha/ tools/
"""


def _job_steps(workflow: dict) -> list[dict]:
    """The steps of the `lint-python` job. Fail when the job or its steps are absent."""
    jobs = workflow.get("jobs")
    job = jobs.get(JOB_ID) if isinstance(jobs, dict) else None
    assert isinstance(job, dict), f"job {JOB_ID!r} is missing from the workflow"
    steps = job.get("steps")
    assert isinstance(steps, list) and steps, f"job {JOB_ID!r} has no steps"
    return [step for step in steps if isinstance(step, dict)]


def _ruff_invocations(run: str) -> list[tuple[str, list[str]]]:
    """Every `ruff <subcommand> <args>` in a shell script, as (subcommand, args)."""
    found: list[tuple[str, list[str]]] = []
    if "ruff" not in run:
        return found
    tokens = shlex.split(run, comments=True)
    for index, token in enumerate(tokens[:-1]):
        if token != "ruff":
            continue
        args: list[str] = []
        for arg in tokens[index + 2 :]:
            if arg in SHELL_OPERATORS:
                break
            args.append(arg)
        found.append((tokens[index + 1], args))
    return found


def _paths(args: list[str]) -> set[str]:
    """The path arguments: every argument that is not an option, without a final slash."""
    return {arg.rstrip("/") for arg in args if not arg.startswith("-")}


def _steps_running(steps: list[dict], subcommand: str, option: str | None = None):
    """Steps with a `ruff <subcommand>` call, and the path set of that call."""
    found = []
    for step in steps:
        for name, args in _ruff_invocations(str(step.get("run") or "")):
            if name == subcommand and (option is None or option in args):
                found.append((step, _paths(args)))
    return found


def format_step_violations(workflow: dict) -> list[str]:
    """Reasons why the job does not check the format of the whole tree. Empty means ok."""
    steps = _job_steps(workflow)
    format_steps = _steps_running(steps, "format", "--check")
    check_steps = _steps_running(steps, "check")
    problems: list[str] = []
    if len(format_steps) != 1:
        problems.append(
            f"expected exactly 1 `ruff format --check` step, found {len(format_steps)}"
        )
    if len(check_steps) != 1:
        problems.append(
            f"expected exactly 1 `ruff check` step, found {len(check_steps)}"
        )
    if problems:
        return problems
    format_step, format_paths = format_steps[0]
    _, check_paths = check_steps[0]
    if not check_paths:
        problems.append("the `ruff check` step has no path argument")
    missing = sorted(check_paths - format_paths)
    extra = sorted(format_paths - check_paths)
    if missing:
        problems.append(f"`ruff format --check` does not cover: {', '.join(missing)}")
    if extra:
        problems.append(
            f"`ruff format --check` has paths that `ruff check` lacks: {', '.join(extra)}"
        )
    if "if" in format_step:
        problems.append(
            f"the `ruff format --check` step has a condition that can skip it: {format_step['if']!r}"
        )
    return problems


def legacy_mechanism_violations(workflow: dict) -> list[str]:
    """Remains of the changed-file mechanism in the job. Empty means none."""
    steps = _job_steps(workflow)
    problems: list[str] = []
    if any(step.get("id") == LEGACY_STEP_ID for step in steps):
        problems.append(f"a step has the id {LEGACY_STEP_ID!r}")
    if f"steps.{LEGACY_STEP_ID}" in json.dumps(steps):
        problems.append(f"a step refers to steps.{LEGACY_STEP_ID}")
    return problems


def _wf(text: str) -> dict:
    return yaml.safe_load(text)


def _ruff_pin(workflow: dict) -> str:
    """The ruff version that the job installs, read from the workflow."""
    pins = {
        match.group(1)
        for step in _job_steps(workflow)
        for match in RUFF_PIN_RE.finditer(str(step.get("run") or ""))
    }
    assert len(pins) == 1, (
        f"expected one ruff pin in job {JOB_ID!r}, found {sorted(pins)}"
    )
    return pins.pop()


def _pinned_ruff_format_check(directory: Path) -> subprocess.CompletedProcess[str]:
    """Run `ruff format --check` on a directory with the ruff version of the workflow."""
    uv = shutil.which("uv")
    assert uv, "uv is required to run the pinned ruff version"
    pin = _ruff_pin(ci_spec.load_workflow(INFRA_CI))
    return subprocess.run(
        [
            uv,
            "tool",
            "run",
            "--from",
            f"ruff=={pin}",
            "ruff",
            "format",
            "--check",
            "--isolated",
            str(directory),
        ],
        capture_output=True,
        text=True,
        cwd=directory,
        timeout=120,
        check=False,
    )


# --- the real workflow --------------------------------------------------------


def test_format_check_covers_the_paths_of_ruff_check() -> None:
    """The format step has the same path set as the lint step and no skip condition."""
    assert format_step_violations(ci_spec.load_workflow(INFRA_CI)) == []


def test_changed_file_mechanism_is_removed() -> None:
    """No step has the id `changed-python`. No step refers to its outputs."""
    assert legacy_mechanism_violations(ci_spec.load_workflow(INFRA_CI)) == []


def test_ruff_format_check_exits_1_for_an_unformatted_file(tmp_path: Path) -> None:
    """The command of the job turns red on a bad file. A usage error exits 2, not 1."""
    (tmp_path / "bad.py").write_text("x = {  'a':1 }\n", encoding="utf-8")
    result = _pinned_ruff_format_check(tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Would reformat: bad.py" in result.stdout


def test_ruff_format_check_exits_0_for_a_formatted_file(tmp_path: Path) -> None:
    (tmp_path / "good.py").write_text('x = {"a": 1}\n', encoding="utf-8")
    result = _pinned_ruff_format_check(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 file already formatted" in result.stdout


# --- the checkers reject broken workflows -------------------------------------


def test_checkers_accept_the_whole_tree_shape() -> None:
    assert format_step_violations(_wf(WHOLE_TREE_JOB)) == []
    assert legacy_mechanism_violations(_wf(WHOLE_TREE_JOB)) == []


def test_checkers_reject_the_changed_file_shape() -> None:
    """The shape that ran no check: a path list from a step output and a skip condition."""
    workflow = _wf(LEGACY_JOB)
    format_problems = format_step_violations(workflow)
    assert any("does not cover: bootstrap" in problem for problem in format_problems)
    assert any("condition that can skip it" in problem for problem in format_problems)
    assert legacy_mechanism_violations(workflow) == [
        "a step has the id 'changed-python'",
        "a step refers to steps.changed-python",
    ]


def test_checker_rejects_a_format_step_that_misses_a_directory() -> None:
    mutated = WHOLE_TREE_JOB.replace(
        "ruff format --check libs/ bootstrap/ platform/ finance_report/ truealpha/ tools/",
        "ruff format --check libs/ bootstrap/ platform/ finance_report/ truealpha/",
    )
    assert format_step_violations(_wf(mutated)) == [
        "`ruff format --check` does not cover: tools"
    ]


def test_checker_rejects_a_format_step_with_an_extra_directory() -> None:
    mutated = WHOLE_TREE_JOB.replace(
        "ruff format --check libs/", "ruff format --check docs/ libs/"
    )
    assert format_step_violations(_wf(mutated)) == [
        "`ruff format --check` has paths that `ruff check` lacks: docs"
    ]


def test_checker_rejects_a_format_step_with_no_path() -> None:
    mutated = WHOLE_TREE_JOB.replace(
        "ruff format --check libs/ bootstrap/ platform/ finance_report/ truealpha/ tools/",
        "ruff format --check",
    )
    assert format_step_violations(_wf(mutated)) == [
        "`ruff format --check` does not cover: "
        "bootstrap, finance_report, libs, platform, tools, truealpha"
    ]


def test_checker_rejects_a_skip_condition_on_the_format_step() -> None:
    mutated = WHOLE_TREE_JOB.replace(
        "      - name: Run ruff format check\n",
        "      - name: Run ruff format check\n        if: github.event_name == 'push'\n",
    )
    assert format_step_violations(_wf(mutated)) == [
        "the `ruff format --check` step has a condition that can skip it: "
        "\"github.event_name == 'push'\""
    ]


def test_checker_rejects_a_job_without_a_format_step() -> None:
    mutated = "\n".join(
        line for line in WHOLE_TREE_JOB.splitlines() if "ruff format" not in line
    )
    assert format_step_violations(_wf(mutated)) == [
        "expected exactly 1 `ruff format --check` step, found 0"
    ]


def test_checker_does_not_count_ruff_format_without_check() -> None:
    """`ruff format <paths>` rewrites files. It cannot fail the job on a bad file."""
    mutated = WHOLE_TREE_JOB.replace("ruff format --check", "ruff format")
    assert format_step_violations(_wf(mutated)) == [
        "expected exactly 1 `ruff format --check` step, found 0"
    ]


def test_checkers_fail_when_the_job_is_missing() -> None:
    with pytest.raises(AssertionError, match="job 'lint-python' is missing"):
        format_step_violations({"jobs": {"other": {"steps": [{"run": "true"}]}}})
