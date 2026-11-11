"""A push to main reuses a green pull-request run only for the same tree (#1602).

`tools/ci_tree_reuse.py` must print a run id only when every condition of its
docstring holds, and must print nothing on any error, so that the shards run.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import urllib.error
from pathlib import Path

import pytest
import yaml

from tools import ci_tree_reuse as reuse

REPO = "owner/repo"
TREE = "0123456789abcdef0123456789abcdef01234567"
RUN_ID = 4242


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
    base = {"path": ".github/workflows/infra-ci.yml", "event": "pull_request"}
    base.update(overrides)
    return base


SHARDS = {
    "artifacts": [
        {"name": "infra2-coverage-data-shard1", "expired": False},
        {"name": "infra2-coverage-data-shard2", "expired": False},
    ]
}


class _FakeApi:
    """Answers each request path from a table and records the paths."""

    def __init__(self, table: dict[str, object]) -> None:
        self.table = table
        self.paths: list[str] = []

    def __call__(self, request, timeout):  # noqa: ARG002 - urlopen signature
        path = request.full_url.removeprefix(reuse.API)
        self.paths.append(path)
        for prefix, answer in self.table.items():
            if path.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return io.BytesIO(json.dumps(answer).encode())
        raise AssertionError(f"unexpected request {path}")


def _api(markers=None, run=None, shards=None) -> _FakeApi:
    return _FakeApi(
        {
            f"/repos/{REPO}/actions/artifacts?": {
                "artifacts": [_marker()] if markers is None else markers
            },
            f"/repos/{REPO}/actions/runs/{RUN_ID}/artifacts": (
                SHARDS if shards is None else shards
            ),
            f"/repos/{REPO}/actions/runs/{RUN_ID}": _run() if run is None else run,
        }
    )


def test_a_green_pull_request_run_of_the_same_tree_is_reused() -> None:
    api = _api()
    assert reuse.find_reusable_run(REPO, TREE, "token", opener=api) == str(RUN_ID)
    assert f"name=unit-green-{TREE}" in api.paths[0]


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
    assert (
        reuse.find_reusable_run(REPO, TREE, "t", opener=_api(markers=markers)) == ""
    ), reason


@pytest.mark.parametrize(
    "run",
    [
        _run(path=".github/workflows/ops-checks.yml"),
        _run(event="push"),
        _run(event="workflow_dispatch"),
    ],
)
def test_a_run_of_another_workflow_or_event_is_not_reused(run) -> None:
    assert reuse.find_reusable_run(REPO, TREE, "t", opener=_api(run=run)) == ""


@pytest.mark.parametrize(
    "shards",
    [
        {"artifacts": []},
        {"artifacts": [{"name": "infra2-coverage-data-shard1", "expired": True}]},
        {"artifacts": [{"name": "infra2-coverage-context", "expired": False}]},
    ],
)
def test_a_run_without_live_shard_coverage_is_not_reused(shards) -> None:
    assert reuse.find_reusable_run(REPO, TREE, "t", opener=_api(shards=shards)) == ""


def test_an_api_error_means_no_reuse() -> None:
    api = _FakeApi(
        {
            f"/repos/{REPO}/actions/artifacts?": urllib.error.HTTPError(
                "u", 503, "unavailable", {}, None
            )
        }
    )
    assert reuse.find_reusable_run(REPO, TREE, "t", opener=api) == ""


@pytest.mark.parametrize(
    ("repo", "tree", "token"),
    [("", TREE, "t"), (REPO, "", "t"), (REPO, TREE, ""), (REPO, "HEAD^{tree}", "t")],
)
def test_missing_or_malformed_input_means_no_reuse(repo, tree, token) -> None:
    def never(*_a, **_k):
        raise AssertionError("no request may be sent")

    assert reuse.find_reusable_run(repo, tree, token, opener=never) == ""


def test_the_newest_qualifying_marker_wins() -> None:
    older = _marker(
        created_at="2026-11-10T00:00:00Z",
        workflow_run={"id": 1, "repository_id": 7, "head_repository_id": 7},
    )
    api = _api(markers=[older, _marker()])
    assert reuse.find_reusable_run(REPO, TREE, "t", opener=api) == str(RUN_ID)


# --- The workflow wiring (.github/workflows/infra-ci.yml) ---------------------------

WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "infra-ci.yml"
)
REUSE_OUTPUT = "needs.detect-changes.outputs.reuse_run_id"


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _step(job: str, name: str) -> dict:
    return next(s for s in _jobs()[job]["steps"] if s.get("name") == name)


def test_only_a_push_to_main_looks_up_a_reusable_run() -> None:
    job = _jobs()["detect-changes"]
    step = _step("detect-changes", "Find a green pull-request run of the same tree")
    assert (
        step["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    )
    assert "git rev-parse 'HEAD^{tree}'" in step["run"]
    assert "tools/ci_tree_reuse.py" in step["run"]
    assert job["outputs"]["reuse_run_id"] == "${{ steps.reuse.outputs.run_id }}"
    assert job["permissions"]["actions"] == "read"


def test_the_shards_are_skipped_only_when_a_run_is_reused() -> None:
    condition = _jobs()["test-unit-shard"]["if"]
    assert f"{REUSE_OUTPUT} == ''" in condition
    assert "has_non_doc == 'true'" in condition


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
    script = _step("test-deployer-logic", "Require every unit-test shard to succeed")[
        "run"
    ]
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
    assert reused["with"]["pattern"] == own["with"]["pattern"]
    assert own["with"]["pattern"] == f"{reuse.SHARD_ARTIFACT_PREFIX}*"
    assert _jobs()["test-deployer-logic"]["permissions"]["actions"] == "read"


def test_the_marker_is_written_after_every_gate_of_a_green_pull_request_run() -> None:
    steps = _jobs()["test-deployer-logic"]["steps"]
    names = [s.get("name", "") for s in steps]
    record = names.index("Record the tested tree")
    upload = names.index("Upload the tested-tree marker")
    gates = [i for i, n in enumerate(names) if n.startswith(("Gate ", "Audit "))]
    assert len(gates) >= 3 and record > max(gates) and upload > record
    assert "github.event_name == 'pull_request'" in steps[record]["if"]
    assert "needs.test-unit-shard.result == 'success'" in steps[record]["if"]
    assert "git rev-parse 'HEAD^{tree}'" in steps[record]["run"]
    marker = steps[upload]
    assert (
        marker["with"]["name"] == reuse.MARKER_PREFIX + "${{ steps.tree.outputs.sha }}"
    )
    assert marker["continue-on-error"] is True
