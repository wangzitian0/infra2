"""A push to main reuses a green pull-request run only for the same tree (#1602).

`tools/ci_tree_reuse.py` must print a run id only when every condition of its
docstring holds, and must print nothing on any error, so that the shards run.

The fake API records a request it has no answer for instead of raising: the tool
catches every exception and returns "", so a raised assertion would read as a pass.
Each test asserts `api.unexpected == []`.
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import urllib.error
from pathlib import Path

import pytest
import yaml

from tools import ci_tree_reuse as reuse

REPO = "owner/repo"
TREE = "0123456789abcdef0123456789abcdef01234567"
COMMIT = "89abcdef0123456789abcdef0123456789abcdef"
PR_HEAD = "fedcba9876543210fedcba9876543210fedcba98"
RUN_ID = 4242
SHARDS_3 = tuple(f"infra2-coverage-data-shard{k}" for k in (1, 2, 3))


def _marker(**overrides) -> dict:
    base = {
        "name": f"unit-green-{TREE}",
        "expired": False,
        "created_at": "2026-11-11T10:00:00Z",
        "workflow_run": {"id": RUN_ID, "repository_id": 7, "head_repository_id": 7},
    }
    base.update(overrides)
    return base


def _run(**overrides) -> dict:
    base = {
        "path": ".github/workflows/infra-ci.yml",
        "event": "pull_request",
        "head_sha": PR_HEAD,
    }
    base.update(overrides)
    return base


def _pull(**overrides) -> dict:
    base = {
        "number": 1,
        "merge_commit_sha": COMMIT,
        "merged_at": "2026-11-11T11:00:00Z",
        "head": {"sha": PR_HEAD},
    }
    base.update(overrides)
    return base


def _jobs(*conclusions: str, total: int | None = None) -> dict:
    total = len(conclusions) if total is None else total
    shards = [
        {"name": f"Unit tests shard {k}/{total}", "conclusion": c}
        for k, c in enumerate(conclusions, start=1)
    ]
    return {"jobs": shards + [{"name": "Lint Python Code", "conclusion": "success"}]}


def _shard_artifacts(*names: str, expired: tuple[str, ...] = ()) -> dict:
    return {
        "artifacts": [{"name": n, "expired": n in expired} for n in names]
        + [{"name": "infra2-coverage-context", "expired": False}]
    }


class _FakeApi:
    """Answers each request path from a table; records every path and every miss."""

    def __init__(self, table: dict[str, object]) -> None:
        self.table = table
        self.paths: list[str] = []
        self.unexpected: list[str] = []

    def __call__(self, request, timeout):  # noqa: ARG002 - urlopen signature
        path = request.full_url.removeprefix(reuse.API)
        self.paths.append(path)
        for prefix, answer in self.table.items():
            if path.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return io.BytesIO(json.dumps(answer).encode())
        self.unexpected.append(path)
        raise urllib.error.URLError(f"no fake answer for {path}")


def _api(pulls=None, markers=None, run=None, jobs=None, artifacts=None) -> _FakeApi:
    return _FakeApi(
        {
            f"/repos/{REPO}/commits/{COMMIT}/pulls": [_pull()]
            if pulls is None
            else pulls,
            f"/repos/{REPO}/actions/artifacts?": {
                "artifacts": [_marker()] if markers is None else markers
            },
            f"/repos/{REPO}/actions/runs/{RUN_ID}/jobs": (
                _jobs("success", "success", "success") if jobs is None else jobs
            ),
            f"/repos/{REPO}/actions/runs/{RUN_ID}/artifacts": (
                _shard_artifacts(*SHARDS_3) if artifacts is None else artifacts
            ),
            f"/repos/{REPO}/actions/runs/{RUN_ID}": _run() if run is None else run,
        }
    )


def _find(api: _FakeApi) -> str:
    return reuse.find_reusable_run(REPO, TREE, COMMIT, "token", opener=api)


def test_a_green_run_of_the_merged_pull_request_with_the_same_tree_is_reused() -> None:
    api = _api()
    assert _find(api) == str(RUN_ID)
    assert api.unexpected == []
    assert any(f"name=unit-green-{TREE}" in p for p in api.paths)


@pytest.mark.parametrize(
    ("markers", "reason"),
    [
        ([], "no marker"),
        ([_marker(expired=True)], "expired marker"),
        ([_marker(name="unit-green-ffff")], "marker of another tree"),
        (
            [
                _marker(
                    workflow_run={
                        "id": RUN_ID,
                        "repository_id": 7,
                        "head_repository_id": 9,
                    }
                )
            ],
            "run from a fork",
        ),
        ([_marker(workflow_run=None)], "marker without a run"),
    ],
)
def test_a_marker_that_does_not_qualify_is_not_reused(markers, reason) -> None:
    api = _api(markers=markers)
    assert _find(api) == "", reason
    assert api.unexpected == []


@pytest.mark.parametrize(
    "run",
    [
        _run(path=".github/workflows/ops-checks.yml"),
        _run(event="push"),
        _run(event="workflow_dispatch"),
    ],
)
def test_a_run_of_another_workflow_or_event_is_not_reused(run) -> None:
    api = _api(run=run)
    assert _find(api) == ""
    assert api.unexpected == []


def test_a_run_of_another_pull_request_head_is_not_reused() -> None:
    # Another pull request's run can upload a marker for this tree; it is not this merge's evidence.
    api = _api(run=_run(head_sha="1" * 40))
    assert _find(api) == ""
    assert api.unexpected == []


@pytest.mark.parametrize(
    ("pulls", "reason"),
    [
        ([], "a direct push: no pull request"),
        (
            [_pull(merge_commit_sha="2" * 40)],
            "the pull request was merged as another commit",
        ),
        ([_pull(merged_at=None)], "the pull request is not merged"),
        ([_pull(), _pull(number=2, head={"sha": "3" * 40})], "two merged heads"),
    ],
)
def test_a_push_that_is_not_one_merged_pull_request_is_not_reused(
    pulls, reason
) -> None:
    api = _api(pulls=pulls)
    assert _find(api) == "", reason
    assert api.unexpected == []
    assert not any("/actions/" in p for p in api.paths), (
        "no artifact lookup without a PR"
    )


@pytest.mark.parametrize(
    ("jobs", "artifacts", "reason"),
    [
        (_jobs("success", "failure", "success"), None, "a shard failed"),
        (_jobs("success", "success", total=3), None, "a shard job is missing"),
        ({"jobs": []}, None, "no shard job"),
        (None, _shard_artifacts(*SHARDS_3[:2]), "a shard artifact is missing"),
        (None, _shard_artifacts(*SHARDS_3, expired=(SHARDS_3[2],)), "a shard expired"),
    ],
)
def test_a_run_without_every_shard_green_and_live_is_not_reused(
    jobs, artifacts, reason
) -> None:
    api = _api(jobs=jobs, artifacts=artifacts)
    assert _find(api) == "", reason
    assert api.unexpected == []


def test_an_api_error_means_no_reuse_and_writes_nothing_to_stdout(capsys) -> None:
    # Stdout becomes the step output: the error message must go to stderr only.
    error = urllib.error.HTTPError("u", 503, "unavailable", {}, None)
    api = _FakeApi({f"/repos/{REPO}/commits/{COMMIT}/pulls": error})
    assert _find(api) == ""
    assert api.unexpected == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no reuse" in captured.err


@pytest.mark.parametrize(
    ("repo", "tree", "commit", "token"),
    [
        ("", TREE, COMMIT, "t"),
        (REPO, "", COMMIT, "t"),
        (REPO, TREE, "", "t"),
        (REPO, TREE, COMMIT, ""),
        (REPO, "HEAD^{tree}", COMMIT, "t"),
        (REPO, TREE, "abc", "t"),
    ],
)
def test_missing_or_malformed_input_sends_no_request(repo, tree, commit, token) -> None:
    api = _api()
    assert reuse.find_reusable_run(repo, tree, commit, token, opener=api) == ""
    assert api.paths == []


def test_the_newest_qualifying_marker_wins() -> None:
    older = _marker(
        created_at="2026-11-10T00:00:00Z",
        workflow_run={"id": 1, "repository_id": 7, "head_repository_id": 7},
    )
    api = _api(markers=[older, _marker()])
    assert _find(api) == str(RUN_ID)
    assert api.unexpected == []


@pytest.mark.parametrize(("found", "stdout"), [("4242", "4242\n"), ("", "")])
def test_the_cli_prints_only_the_run_id(monkeypatch, capsys, found, stdout) -> None:
    monkeypatch.setattr(reuse, "find_reusable_run", lambda *a, **k: found)
    assert reuse.main(["--repo", REPO, "--tree", TREE, "--commit", COMMIT]) == 0
    assert capsys.readouterr().out == stdout


# --- The workflow wiring (.github/workflows/infra-ci.yml) ---------------------------

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "infra-ci.yml"
REUSE_OUTPUT = "needs.detect-changes.outputs.reuse_run_id"
LOOKUP_STEP = "Find a green pull-request run of the same tree"
RESULT_STEP = "Require every unit-test shard to succeed, or a reused green run"


def _workflow_jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _step(job: str, name: str) -> dict:
    return next(s for s in _workflow_jobs()[job]["steps"] if s.get("name") == name)


def test_only_a_push_to_main_looks_up_a_reusable_run() -> None:
    job = _workflow_jobs()["detect-changes"]
    step = _step("detect-changes", LOOKUP_STEP)
    assert (
        step["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    )
    assert step["id"] == "reuse"
    assert job["outputs"]["reuse_run_id"] == "${{ steps.reuse.outputs.run_id }}"
    assert job["permissions"]["actions"] == "read"
    # GET /commits/{sha}/pulls needs it once the repository is private.
    assert job["permissions"]["pull-requests"] == "read"


@pytest.mark.parametrize(
    ("tool_stdout", "expected"),
    [("4242", "4242"), ("none", ""), ("ci_tree_reuse: no reuse (x)", ""), ("", "")],
)
def test_the_lookup_step_passes_on_only_a_numeric_run_id(
    tmp_path: Path, tool_stdout: str, expected: str
) -> None:
    script = _step("detect-changes", LOOKUP_STEP)["run"].replace(
        "${{ github.repository }}", REPO
    )
    fake = tmp_path / "bin" / "python3"
    fake.parent.mkdir()
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$@" > "$FAKE_ARGS"\n'
        f"printf '%s\\n' {json.dumps(tool_stdout)}\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    output, args = tmp_path / "out", tmp_path / "args"
    env = {
        **os.environ,
        "PATH": f"{fake.parent}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(output),
        "FAKE_ARGS": str(args),
    }
    completed = subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert output.read_text(encoding="utf-8") == f"run_id={expected}\n"
    passed = args.read_text(encoding="utf-8").split()
    assert passed[:3] == ["tools/ci_tree_reuse.py", "--repo", REPO]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert passed[passed.index("--commit") + 1] == head


def test_the_shards_are_skipped_only_when_a_run_is_reused() -> None:
    assert _workflow_jobs()["test-unit-shard"]["if"] == (
        f"needs.detect-changes.outputs.has_non_doc == 'true' && {REUSE_OUTPUT} == ''"
    )


@pytest.mark.parametrize(
    ("reuse_run_id", "result", "expected_code"),
    [
        ("", "success", 0),
        ("", "skipped", 1),
        ("", "failure", 1),
        ("4242", "skipped", 0),
        ("4242", "success", 1),
        ("4242", "failure", 1),
        ("4242", "cancelled", 1),
    ],
)
def test_the_required_check_accepts_skipped_shards_only_with_a_reused_run(
    reuse_run_id: str, result: str, expected_code: int
) -> None:
    script = _step("test-deployer-logic", RESULT_STEP)["run"]
    env = {**os.environ, "SHARD_RESULT": result, "REUSE_RUN_ID": reuse_run_id}
    completed = subprocess.run(
        ["bash", "-e", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == expected_code, completed.stdout + completed.stderr


def test_the_coverage_comes_from_this_run_or_from_the_reused_run() -> None:
    own = _step("test-deployer-logic", "Download coverage data of the shards")
    reused = _step(
        "test-deployer-logic", "Download coverage data of the reused pull-request run"
    )
    assert own["if"] == f"{REUSE_OUTPUT} == ''"
    assert reused["if"] == f"{REUSE_OUTPUT} != ''"
    assert reused["with"]["run-id"] == "${{ " + REUSE_OUTPUT + " }}"
    # Without the token, download-artifact ignores run-id and reads the current run.
    assert reused["with"]["github-token"] == "${{ github.token }}"
    assert reused["with"]["pattern"] == own["with"]["pattern"]
    assert own["with"]["pattern"] == f"{reuse.SHARD_ARTIFACT_PREFIX}*"
    assert _workflow_jobs()["test-deployer-logic"]["permissions"]["actions"] == "read"


def test_the_marker_is_written_after_every_gate_of_a_green_pull_request_run() -> None:
    steps = _workflow_jobs()["test-deployer-logic"]["steps"]
    names = [s.get("name", "") for s in steps]
    record = names.index("Record the tested tree")
    upload = names.index("Upload the tested-tree marker")
    gates = [i for i, n in enumerate(names) if n.startswith(("Gate ", "Audit "))]
    assert len(gates) >= 3 and record > max(gates) and upload > record
    # No always(): a failed gate above must skip the marker.
    assert steps[record]["if"] == (
        "github.event_name == 'pull_request' && needs.test-unit-shard.result == 'success'"
    )
    assert steps[record]["id"] == "tree"
    assert "tree=\"$(git rev-parse 'HEAD^{tree}')\"" in steps[record]["run"]
    assert 'echo "sha=${tree}" >> "$GITHUB_OUTPUT"' in steps[record]["run"]
    marker = steps[upload]
    assert (
        marker["with"]["name"] == reuse.MARKER_PREFIX + "${{ steps.tree.outputs.sha }}"
    )
    assert marker["continue-on-error"] is True
