"""Contracts for the sharded unit-test run in infra-ci (#1499)."""

from __future__ import annotations

import importlib.util
import json
import os
import statistics
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
PREFLIGHT_STEP = "Gate preflight static, SSOT, and drift checks"
SHARD_COUNT = 3
FILE_COUNT = 9
TESTS_PER_FILE = 5


def _plugin():
    spec = importlib.util.spec_from_file_location(
        "pytest_shard_under_test", PLUGIN_DIR / "pytest_shard.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(
    targets: list[Path],
    shard: str | None,
    cwd: Path,
    *extra: str,
    collect_only: bool = True,
) -> tuple[set[str], subprocess.CompletedProcess[str]]:
    cmd = [sys.executable, "-m", "pytest", *map(str, targets), "-q", "-n", "0"]
    if collect_only:
        cmd.append("--collect-only")
    cmd += [
        "-p",
        "no:cacheprovider",
        "-p",
        "pytest_shard",
        "-c",
        os.devnull,
        "--rootdir",
        str(cwd),
        f"--shard-weights={cwd / 'absent-weights.json'}",
        *extra,
    ]
    if shard is not None:
        cmd.append(f"--shard={shard}")
    env = {**os.environ, "PYTHONPATH": str(PLUGIN_DIR), "PYTHONSAFEPATH": "1"}
    result = subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=120
    )
    ids = {line for line in result.stdout.splitlines() if "::" in line}
    return ids, result


def _files_of(ids: set[str]) -> set[str]:
    return {i.split("::", 1)[0] for i in ids}


@pytest.fixture(scope="module")
def many_files(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("shard_sample")
    body = "\n".join(f"def test_case_{i}():\n    pass\n" for i in range(TESTS_PER_FILE))
    for n in range(FILE_COUNT):
        (directory / f"test_file_{n}.py").write_text(body, encoding="utf-8")
    return directory


def test_shards_are_complete_disjoint_and_not_empty(many_files: Path) -> None:
    full, full_run = _run([many_files], None, many_files)
    assert len(full) == FILE_COUNT * TESTS_PER_FILE, full_run.stdout + full_run.stderr
    shards = []
    for k in range(1, SHARD_COUNT + 1):
        ids, run = _run([many_files], f"{k}/{SHARD_COUNT}", many_files)
        assert ids, f"shard {k} is empty: {run.stdout}{run.stderr}"
        shards.append(ids)
    for a in range(SHARD_COUNT):
        for b in range(a + 1, SHARD_COUNT):
            assert not (shards[a] & shards[b]), f"shards {a + 1} and {b + 1} overlap"
            assert not (_files_of(shards[a]) & _files_of(shards[b])), (
                f"shards {a + 1} and {b + 1} split a test file"
            )
    assert set().union(*shards) == full


def test_a_shard_does_not_import_the_test_files_of_other_shards(
    many_files: Path, tmp_path: Path
) -> None:
    # A node-id split collects every file in every shard, so all shards would fail here.
    for path in many_files.glob("test_*.py"):
        (tmp_path / path.name).write_text(path.read_text(encoding="utf-8"))
    (tmp_path / "test_poison.py").write_text(
        'raise RuntimeError("poison file imported")\n', encoding="utf-8"
    )
    failed = []
    for k in range(1, SHARD_COUNT + 1):
        _, run = _run([tmp_path], f"{k}/{SHARD_COUNT}", tmp_path)
        if "poison file imported" in run.stdout + run.stderr:
            assert run.returncode != 0
            failed.append(k)
        else:
            assert run.returncode == 0, run.stdout + run.stderr
    assert len(failed) == 1, f"shards that imported the poison file: {failed}"


def test_shard_split_is_the_same_in_every_process(many_files: Path) -> None:
    first, _ = _run([many_files], f"1/{SHARD_COUNT}", many_files)
    second, _ = _run([many_files], f"1/{SHARD_COUNT}", many_files)
    assert first and first == second


def test_test_files_named_on_the_command_line_are_split_by_file(
    many_files: Path,
) -> None:
    # pytest does not ask pytest_ignore_collect about a named path; the backstop drops it.
    named = sorted(many_files.glob("test_*.py"))
    owners: dict[str, list[int]] = {}
    for k in range(1, SHARD_COUNT + 1):
        ids, run = _run(named, f"{k}/{SHARD_COUNT}", many_files)
        assert run.returncode in (0, 5), run.stdout + run.stderr
        for name in _files_of(ids):
            owners.setdefault(name, []).append(k)
    assert sorted(owners) == sorted(p.name for p in named)
    assert all(len(shards) == 1 for shards in owners.values()), owners


@pytest.mark.parametrize("value", ["0/2", "3/2", "x", "1/0", "1"])
def test_a_malformed_shard_option_fails_the_run(many_files: Path, value: str) -> None:
    _, run = _run([many_files], value, many_files)
    assert run.returncode != 0
    assert "--shard" in run.stdout + run.stderr


def test_longest_files_first_balances_the_shards() -> None:
    weights = {"a": 10.0, "b": 9.0, "c": 8.0, "d": 1.0, "e": 1.0, "f": 1.0}
    shard = _plugin().assign(weights, weights, 3)
    loads = [sum(w for f, w in weights.items() if shard[f] == k) for k in (1, 2, 3)]
    assert loads == [10.0, 10.0, 10.0], shard


def test_a_file_without_a_weight_gets_the_median_weight() -> None:
    # Median 4.0 puts "z" in shard 2 (loads 8 and 8); a default of 1.0 puts it in shard 1.
    shard = _plugin().assign(["a", "b", "c", "z"], {"a": 4.0, "b": 2.0, "c": 6.0}, 2)
    assert shard == {"c": 1, "a": 2, "z": 2, "b": 1}


def test_the_assignment_does_not_depend_on_the_input_order() -> None:
    weights = {f"f{i}": float(i % 4) for i in range(20)}
    plugin = _plugin()
    files = sorted(weights)
    assert plugin.assign(files, weights, 3) == plugin.assign(files[::-1], weights, 3)


@pytest.mark.parametrize("body", ["[1, 2]", '{"a": -1}', '{"a": "slow"}'])
def test_bad_shard_weights_fail_the_run(
    many_files: Path, tmp_path: Path, body: str
) -> None:
    weights = tmp_path / "weights.json"
    weights.write_text(body, encoding="utf-8")
    _, run = _run(
        [many_files], f"1/{SHARD_COUNT}", many_files, f"--shard-weights={weights}"
    )
    assert run.returncode != 0
    # The plugin's own message names the file; a crash deeper in json or float does not.
    assert f"{weights}: " in run.stdout + run.stderr


def test_the_committed_weights_balance_the_real_test_files() -> None:
    plugin = _plugin()
    count = len(_workflow()["jobs"][SHARD_JOB]["strategy"]["matrix"]["shard"])
    weights = plugin.load_weights(plugin.DEFAULT_WEIGHTS)
    assert weights, f"{plugin.DEFAULT_WEIGHTS} holds no weights"
    files = sorted(
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "common" / "tests").rglob("test_*.py")
        if "__pycache__" not in p.parts
    )
    shard = plugin.assign(files, weights, count)
    default = statistics.median(weights.values())
    loads = [
        sum(weights.get(f, default) for f in files if shard[f] == k)
        for k in range(1, count + 1)
    ]
    assert all(loads), loads
    # Bound of longest-processing-time first: no shard exceeds another by the largest file.
    assert max(loads) - min(loads) <= max(weights.values()), loads


def test_the_durations_out_option_writes_the_seconds_of_each_file(
    many_files: Path, tmp_path: Path
) -> None:
    out = tmp_path / "durations.json"
    _, run = _run(
        [many_files],
        None,
        many_files,
        f"--shard-durations-out={out}",
        collect_only=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    seconds = json.loads(out.read_text(encoding="utf-8"))
    assert sorted(seconds) == sorted(p.name for p in many_files.glob("test_*.py"))
    assert all(isinstance(s, float | int) and s >= 0 for s in seconds.values())


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
    assert names.index(PREFLIGHT_STEP) < names.index("Run infra unit tests")
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


def test_exactly_one_shard_runs_the_preflight() -> None:
    job = _workflow()["jobs"][SHARD_JOB]
    step = next(s for s in job["steps"] if s.get("name") == PREFLIGHT_STEP)
    assert step.get("if") == "matrix.shard == 1"
    assert job["strategy"]["matrix"]["shard"][0] == 1
