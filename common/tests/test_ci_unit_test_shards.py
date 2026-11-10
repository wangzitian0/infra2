"""Contracts for the sharded unit-test run in infra-ci (#1499)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = ROOT / "common" / "ci_plugins"
WORKFLOW = ROOT / ".github" / "workflows" / "infra-ci.yml"
SHARD_JOB = "test-unit-shard"
FINAL_JOB = "test-deployer-logic"
SHARD_COUNT = 3
TEST_COUNT = 60


def _collect(
    test_file: Path, shard: str | None, cwd: Path
) -> tuple[set[str], subprocess.CompletedProcess[str]]:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        str(test_file),
        "--collect-only",
        "-q",
        "-n",
        "0",
        "-p",
        "no:cacheprovider",
        "-p",
        "pytest_shard",
        "-c",
        os.devnull,
        "--rootdir",
        str(cwd),
    ]
    if shard is not None:
        cmd.append(f"--shard={shard}")
    env = {**os.environ, "PYTHONPATH": str(PLUGIN_DIR), "PYTHONSAFEPATH": "1"}
    result = subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=120
    )
    ids = {line for line in result.stdout.splitlines() if "::" in line}
    return ids, result


@pytest.fixture(scope="module")
def many_tests(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    directory = tmp_path_factory.mktemp("shard_sample")
    body = "\n".join(f"def test_case_{i}():\n    pass\n" for i in range(TEST_COUNT))
    path = directory / "test_sample.py"
    path.write_text(body, encoding="utf-8")
    return path, directory


def test_shards_are_complete_disjoint_and_not_empty(
    many_tests: tuple[Path, Path],
) -> None:
    path, directory = many_tests
    full, full_run = _collect(path, None, directory)
    assert len(full) == TEST_COUNT, full_run.stdout + full_run.stderr
    shards = []
    for k in range(1, SHARD_COUNT + 1):
        ids, run = _collect(path, f"{k}/{SHARD_COUNT}", directory)
        assert ids, f"shard {k} is empty: {run.stdout}{run.stderr}"
        shards.append(ids)
    for a in range(SHARD_COUNT):
        for b in range(a + 1, SHARD_COUNT):
            assert not (shards[a] & shards[b]), f"shards {a + 1} and {b + 1} overlap"
    assert set().union(*shards) == full


def test_shard_split_is_the_same_in_every_process(
    many_tests: tuple[Path, Path],
) -> None:
    path, directory = many_tests
    first, _ = _collect(path, f"1/{SHARD_COUNT}", directory)
    second, _ = _collect(path, f"1/{SHARD_COUNT}", directory)
    assert first == second


@pytest.mark.parametrize("value", ["0/2", "3/2", "x", "1/0", "1"])
def test_a_malformed_shard_option_fails_the_run(
    many_tests: tuple[Path, Path], value: str
) -> None:
    path, directory = many_tests
    _, run = _collect(path, value, directory)
    assert run.returncode != 0
    assert "--shard" in run.stdout + run.stderr


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _final_gate_step() -> dict:
    steps = _workflow()["jobs"][FINAL_JOB]["steps"]
    return next(s for s in steps if "SHARD_RESULT" in s.get("env", {}))


def test_final_job_keeps_the_required_name_and_runs_after_a_failed_shard() -> None:
    job = _workflow()["jobs"][FINAL_JOB]
    assert job["name"] == "Test Deployer Hash Logic"
    assert "always()" in job["if"]
    assert "has_non_doc == 'true'" in job["if"]
    assert SHARD_JOB in job["needs"]
    assert "detect-changes" in job["needs"]


def test_final_job_reads_the_shard_job_result() -> None:
    step = _final_gate_step()
    assert step["env"]["SHARD_RESULT"] == f"${{{{ needs.{SHARD_JOB}.result }}}}"
    steps = _workflow()["jobs"][FINAL_JOB]["steps"]
    assert steps[0] is step or steps[0] == step, "the result check must run first"


@pytest.mark.parametrize(
    ("result", "expected_code"),
    [("success", 0), ("failure", 1), ("cancelled", 1), ("skipped", 1), ("", 1)],
)
def test_final_job_check_fails_unless_the_shards_succeeded(
    result: str, expected_code: int
) -> None:
    run_script = _final_gate_step()["run"]
    completed = subprocess.run(
        ["bash", "-e", "-c", run_script],
        env={**os.environ, "SHARD_RESULT": result},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == expected_code, completed.stdout + completed.stderr
    assert f"result: {result}" in completed.stdout


def test_shard_jobs_gate_before_pytest_and_keep_the_coverage_targets() -> None:
    job = _workflow()["jobs"][SHARD_JOB]
    assert job["strategy"]["fail-fast"] is False
    shards = job["strategy"]["matrix"]["shard"]
    assert len(shards) >= 2
    assert shards == list(range(1, len(shards) + 1)), "matrix must be 1..N"
    assert "strategy.job-total" in job["name"]
    names = [s.get("name", "") for s in job["steps"]]
    assert names.index("Gate preflight static, SSOT, and drift checks") < names.index(
        "Run infra unit tests"
    )
    run = next(s for s in job["steps"] if s.get("name") == "Run infra unit tests")
    assert "--shard=${{ matrix.shard }}/${{ strategy.job-total }}" in run["run"]
    assert "/2" not in run["run"].split("--shard=")[1].split()[0]
    assert "--cov=common" in run["run"] and "--cov=tools" in run["run"]
    assert "-n auto" in run["run"]
    upload = next(
        s
        for s in job["steps"]
        if s.get("uses", "").startswith("actions/upload-artifact")
    )
    assert upload["with"]["include-hidden-files"] is True


def test_final_job_combines_the_shard_files_by_glob() -> None:
    steps = _workflow()["jobs"][FINAL_JOB]["steps"]
    combine = next(s for s in steps if "coverage combine" in s.get("run", ""))
    assert "coverage/.coverage.shard*" in combine["run"]
    assert ".coverage.shard1" not in combine["run"]
