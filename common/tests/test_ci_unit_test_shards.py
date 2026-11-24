"""Contracts for the sharded unit-test run in infra-ci (#1499)."""

from __future__ import annotations

import importlib.util
import json
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


def test_shards_are_complete_disjoint_stable_and_not_empty(many_files: Path) -> None:
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
    # The split is the same in every process.
    again, _ = _run([many_files], f"1/{SHARD_COUNT}", many_files)
    assert again == shards[0]


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


def test_a_malformed_option_fails_the_run(many_files: Path, tmp_path: Path) -> None:
    for value in ("0/2", "3/2", "x", "1/0", "1"):
        _, run = _run([many_files], value, many_files)
        assert run.returncode != 0, value
        assert "--shard" in run.stdout + run.stderr, value
    weights = tmp_path / "weights.json"
    for body in ("[1, 2]", '{"a": -1}', '{"a": "slow"}'):
        weights.write_text(body, encoding="utf-8")
        _, run = _run(
            [many_files], f"1/{SHARD_COUNT}", many_files, f"--shard-weights={weights}"
        )
        assert run.returncode != 0, body
        # The plugin's own message names the file; a crash deeper in json or float does not.
        assert f"{weights}: " in run.stdout + run.stderr, body


def test_the_assignment_balances_by_weight_and_ignores_input_order() -> None:
    plugin = _plugin()
    weights = {"a": 10.0, "b": 9.0, "c": 8.0, "d": 1.0, "e": 1.0, "f": 1.0}
    shard = plugin.assign(weights, weights, 3)
    loads = [sum(w for f, w in weights.items() if shard[f] == k) for k in (1, 2, 3)]
    assert loads == [10.0, 10.0, 10.0], shard
    # A file without a weight gets the median: 4.0 puts "z" in shard 2 (loads 8 and 8);
    # a default of 1.0 puts it in shard 1.
    shard = plugin.assign(["a", "b", "c", "z"], {"a": 4.0, "b": 2.0, "c": 6.0}, 2)
    assert shard == {"c": 1, "a": 2, "z": 2, "b": 1}
    many = {f"f{i}": float(i % 4) for i in range(20)}
    files = sorted(many)
    assert plugin.assign(files, many, 3) == plugin.assign(files[::-1], many, 3)


def test_a_run_places_the_files_by_the_weights_file(
    many_files: Path, tmp_path: Path
) -> None:
    # One heavy file fills shard 1. Equal default weights or a path hash put other files there.
    weights = tmp_path / "weights.json"
    names = sorted(p.name for p in many_files.glob("test_*.py"))
    heavy = names[-1]
    weights.write_text(
        json.dumps({n: (100.0 if n == heavy else 1.0) for n in names}),
        encoding="utf-8",
    )
    placed = {}
    for k in range(1, SHARD_COUNT + 1):
        ids, run = _run(
            [many_files], f"{k}/{SHARD_COUNT}", many_files, f"--shard-weights={weights}"
        )
        assert run.returncode == 0, run.stdout + run.stderr
        placed[k] = _files_of(ids)
    assert placed[1] == {heavy}
    assert len(placed[2]) == len(placed[3]) == (len(names) - 1) // 2


def test_the_committed_weights_parse_and_use_rootdir_paths() -> None:
    # A key in another form (a bare name, an absolute path) matches no file, so every
    # file would fall back to the median weight and the balance would be lost silently.
    plugin = _plugin()
    weights = plugin.load_weights(plugin.DEFAULT_WEIGHTS)
    assert weights, f"{plugin.DEFAULT_WEIGHTS} holds no weights"
    wrong = [n for n in weights if not n.startswith("common/tests/test_")]
    assert not wrong, f"keys that are not rootdir paths: {wrong[:5]}"


def test_durations_are_written_per_file_and_merged_by_sum(
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
    first, second, merged = (tmp_path / n for n in ("a.json", "b.json", "out.json"))
    first.write_text('{"t_a.py": 1.5, "t_b.py": 2.0}', encoding="utf-8")
    second.write_text('{"t_b.py": 0.25, "t_c.py": 4.0}', encoding="utf-8")
    assert _plugin().main(["merge", str(merged), str(first), str(second)]) == 0
    assert json.loads(merged.read_text(encoding="utf-8")) == {
        "t_a.py": 1.5,
        "t_b.py": 2.25,
        "t_c.py": 4.0,
    }
    # No input file: the merge fails and writes nothing.
    none = tmp_path / "none.json"
    assert _plugin().main(["merge", str(none), str(tmp_path / "absent.json")]) == 1
    assert not none.exists()


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _final_gate_step() -> dict:
    steps = _workflow()["jobs"][FINAL_JOB]["steps"]
    return next(s for s in steps if "SHARD_RESULT" in s.get("env", {}))


def test_final_job_check_fails_unless_the_shards_succeeded() -> None:
    run_script = _final_gate_step()["run"]
    for result, expected_code in (
        ("success", 0),
        ("failure", 1),
        ("cancelled", 1),
        ("skipped", 1),
        ("", 1),
    ):
        completed = subprocess.run(
            ["bash", "-e", "-c", run_script],
            env={**os.environ, "SHARD_RESULT": result},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == expected_code, (
            result,
            completed.stdout + completed.stderr,
        )
        assert f"result: {result}" in completed.stdout


def test_final_job_wiring_runs_after_a_failed_shard_and_reports_weights() -> None:
    job = _workflow()["jobs"][FINAL_JOB]
    assert job["name"] == "Test Deployer Hash Logic"
    assert "always()" in job["if"]
    assert "has_non_doc == 'true'" in job["if"]
    assert SHARD_JOB in job["needs"]
    assert "detect-changes" in job["needs"]
    steps = job["steps"]
    step = _final_gate_step()
    assert step["env"]["SHARD_RESULT"] == f"${{{{ needs.{SHARD_JOB}.result }}}}"
    assert steps[0] == step, "the result check must run first"
    combine = next(s for s in steps if "coverage combine" in s.get("run", ""))
    assert "coverage/.coverage.shard*" in combine["run"]
    assert ".coverage.shard1" not in combine["run"]
    merge = next(s for s in steps if "pytest_shard.py merge" in s.get("run", ""))
    assert "coverage/durations.shard*.json" in merge["run"]
    upload = next(
        s for s in steps if s.get("with", {}).get("name") == "infra2-shard-weights"
    )
    assert upload["with"]["path"] == "coverage/shard_weights.json"
    # The weights report runs after every gate and cannot fail the check: a report
    # for a later commit must not hide a gate or fail the required check.
    names = [s.get("name", "") for s in steps]
    gates = [i for i, n in enumerate(names) if n.startswith(("Gate ", "Audit "))]
    report = [steps.index(merge), steps.index(upload)]
    assert report[0] < report[1] and len(gates) >= 3, (report, gates)
    assert min(report) > max(gates)
    assert all(steps[i].get("continue-on-error") is True for i in report)


def test_shard_jobs_gate_before_pytest_and_keep_the_coverage_targets() -> None:
    job = _workflow()["jobs"][SHARD_JOB]
    assert job["strategy"]["fail-fast"] is False
    shards = job["strategy"]["matrix"]["shard"]
    assert len(shards) >= 2
    assert shards == list(range(1, len(shards) + 1)), "matrix must be 1..N"
    assert "strategy.job-total" in job["name"]
    names = [s.get("name", "") for s in job["steps"]]
    assert names.index(PREFLIGHT_STEP) < names.index("Run infra unit tests")
    # Exactly one shard runs the preflight.
    step = next(s for s in job["steps"] if s.get("name") == PREFLIGHT_STEP)
    assert step.get("if") == "matrix.shard == 1"
    run = next(s for s in job["steps"] if s.get("name") == "Run infra unit tests")
    assert "--shard=${{ matrix.shard }}/${{ strategy.job-total }}" in run["run"]
    assert "/2" not in run["run"].split("--shard=")[1].split()[0]
    assert "--cov=common" in run["run"] and "--cov=tools" in run["run"]
    assert "-n auto" in run["run"]
    # Each shard records and uploads the seconds of its files.
    out = "coverage/durations.shard${{ matrix.shard }}.json"
    assert f"--shard-durations-out={out}" in run["run"]
    upload = next(
        s
        for s in job["steps"]
        if s.get("uses", "").startswith("actions/upload-artifact")
    )
    assert upload["with"]["include-hidden-files"] is True
    assert out in upload["with"]["path"].splitlines()
