"""Tests for CI gate linter and Left-to-Right hierarchy rules (#461, #508)."""

from __future__ import annotations

from pathlib import Path
import subprocess
import pytest
import yaml

from common.core.ci_gate_lint import lint_workflow
from infra2_sdk.ci import (
    GATE_WALL_CLOCK_BUDGET_S,
    MAX_SINGLE_TEST_S,
    SHARD_MAX,
    SHARD_MIN,
)


def test_ci_spec_constants() -> None:
    assert GATE_WALL_CLOCK_BUDGET_S == 90
    assert MAX_SINGLE_TEST_S == 30
    assert SHARD_MIN == 2
    assert SHARD_MAX == 8


def test_lint_clean_workflow(tmp_path: Path) -> None:
    wf = {
        "name": "pr-gate",
        "on": ["pull_request"],
        "jobs": {
            "test": {
                "strategy": {
                    "matrix": {
                        "shard": [1, 2, 3, 4],
                    }
                },
                "steps": [
                    {"name": "Checkout", "uses": "actions/checkout@v4"},
                    {"name": "Run tests", "run": "pytest -q -n auto tests/"},
                ],
            }
        },
    }
    wf_file = tmp_path / "pr_gate.yml"
    wf_file.write_text(yaml.dump(wf), encoding="utf-8")

    errors = lint_workflow(wf_file)
    assert errors == []


def test_lint_excessive_shards_flagged(tmp_path: Path) -> None:
    wf = {
        "name": "pr-gate-bloat",
        "on": {"pull_request": None},
        "jobs": {
            "test": {
                "strategy": {
                    "matrix": {
                        "shard": list(range(1, 17)),  # 16 shards!
                    }
                },
                "steps": [
                    {"run": "pytest -n auto"},
                ],
            }
        },
    }
    wf_file = tmp_path / "pr_bloat.yml"
    wf_file.write_text(yaml.dump(wf), encoding="utf-8")

    errors = lint_workflow(wf_file)
    assert len(errors) == 1
    assert "matrix shard count 16 violates [2, 8]" in errors[0]


@pytest.mark.parametrize(
    ("filename", "job_name", "run_cmd", "expected_error"),
    [
        (
            "pr_bench.yml",
            "Run benchmark",
            "python -m pytest -m benchmark",
            "runs banned gate pattern 'benchmark'",
        ),
        (
            "pr_workers.yml",
            "Run tests",
            "pytest -n 4 tests/",
            "uses hardcoded pytest worker count '-n 4'",
        ),
    ],
)
def test_lint_flags_banned_pr_gate_patterns(
    tmp_path: Path, filename: str, job_name: str, run_cmd: str, expected_error: str
) -> None:
    wf = {
        "name": f"pr-gate-{filename}",
        "on": ["pull_request"],
        "jobs": {"test": {"steps": [{"name": job_name, "run": run_cmd}]}},
    }
    wf_file = tmp_path / filename
    wf_file.write_text(yaml.dump(wf), encoding="utf-8")

    errors = lint_workflow(wf_file)
    assert len(errors) == 1
    assert expected_error in errors[0]


def test_lint_non_pr_workflow_ignored(tmp_path: Path) -> None:
    # Nightly benchmark workflow allows benchmarks and heavy shards
    wf = {
        "name": "nightly-benchmark",
        "on": {"schedule": [{"cron": "0 0 * * *"}]},
        "jobs": {
            "heavy-bench": {
                "strategy": {
                    "matrix": {
                        "shard": list(range(1, 20)),
                    }
                },
                "steps": [
                    {"name": "Run benchmark", "run": "pytest -m benchmark"},
                ],
            }
        },
    }
    wf_file = tmp_path / "nightly.yml"
    wf_file.write_text(yaml.dump(wf), encoding="utf-8")

    errors = lint_workflow(wf_file)
    assert errors == []


def test_main_missing_directory_fails_closed(tmp_path: Path) -> None:
    from common.core.ci_gate_lint import main
    import sys
    from unittest.mock import patch

    non_existent = tmp_path / "does_not_exist"
    with patch.object(sys, "argv", ["ci_gate_lint", str(non_existent)]):
        exit_code = main()
        assert exit_code == 1, "Missing directory must exit 1 (fail-closed)"


def test_reconcile_plan_gate_fetches_pr_base_sha_and_fails_on_diff_error(
    tmp_path: Path,
) -> None:
    """#1622:shallow PR checkouts must fetch the PR base SHA explicitly and fail closed."""
    wf_path = Path(__file__).resolve().parents[2] / ".github/workflows/infra-ci.yml"
    workflow = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["test-deployer-logic"]["steps"]
    step = next(
        item
        for item in steps
        if item.get("name") == "Gate reconcile plan builds for changed files"
    )
    script_template = step["run"]
    assert "origin/main...HEAD" not in script_template
    assert "mapfile" not in script_template
    assert "github.event.pull_request.base.sha" in script_template

    bare_repo = tmp_path / "remote.git"
    work_repo = tmp_path / "work"
    shallow_repo = tmp_path / "shallow"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare_repo)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(bare_repo),
            "config",
            "uploadpack.allowAnySHA1InWant",
            "true",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "clone", str(bare_repo), str(work_repo)],
        check=True,
        capture_output=True,
    )
    for cfg_key, cfg_val in (
        ("user.email", "ci@example.test"),
        ("user.name", "CI Test"),
    ):
        subprocess.run(
            ["git", "-C", str(work_repo), "config", cfg_key, cfg_val],
            check=True,
            capture_output=True,
        )
    (work_repo / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(work_repo), "add", "base.txt"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work_repo), "commit", "-m", "base commit"],
        check=True,
        capture_output=True,
    )
    base_sha = subprocess.check_output(
        ["git", "-C", str(work_repo), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    subprocess.run(
        ["git", "-C", str(work_repo), "push", "-u", "origin", "main"],
        check=True,
        capture_output=True,
    )

    subprocess.run(
        ["git", "-C", str(work_repo), "checkout", "-b", "feature/test"],
        check=True,
        capture_output=True,
    )
    (work_repo / "changed.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(work_repo), "add", "changed.py"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work_repo), "commit", "-m", "pr commit"],
        check=True,
        capture_output=True,
    )
    pr_sha = subprocess.check_output(
        ["git", "-C", str(work_repo), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    subprocess.run(
        ["git", "-C", str(work_repo), "push", "-u", "origin", "feature/test"],
        check=True,
        capture_output=True,
    )

    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            "feature/test",
            f"file://{bare_repo}",
            str(shallow_repo),
        ],
        check=True,
        capture_output=True,
    )

    def _run_step(
        target_base: str, has_non_doc: str = "true"
    ) -> subprocess.CompletedProcess[str]:
        rendered = (
            script_template.replace("${{ github.workspace }}", str(shallow_repo))
            .replace("${{ github.event.pull_request.base.sha }}", target_base)
            .replace("${{ needs.detect-changes.outputs.has_non_doc }}", has_non_doc)
            .replace(
                "uv run --locked python -m tools.reconcile_iac_inputs",
                "printf 'RECONCILE:%s\\n'",
            )
        )
        return subprocess.run(
            ["bash", "-c", rendered],
            capture_output=True,
            text=True,
            check=False,
        )

    ok_res = _run_step(base_sha, has_non_doc="true")
    assert ok_res.returncode == 0, ok_res.stderr
    assert "changed.py" in ok_res.stdout

    empty_res = _run_step(pr_sha, has_non_doc="true")
    assert empty_res.returncode != 0
    assert "zero changed files" in empty_res.stdout + empty_res.stderr

    bad_sha_res = _run_step("deadbeef" * 5, has_non_doc="true")
    assert bad_sha_res.returncode != 0

    sub_src = tmp_path / "sub_src"
    subprocess.run(
        ["git", "init", "-b", "main", str(sub_src)], check=True, capture_output=True
    )
    for cfg_key, cfg_val in (
        ("user.email", "ci@example.test"),
        ("user.name", "CI Test"),
    ):
        subprocess.run(
            ["git", "-C", str(sub_src), "config", cfg_key, cfg_val],
            check=True,
            capture_output=True,
        )
    (sub_src / "README.md").write_text("sub\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(sub_src), "add", "README.md"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(sub_src), "commit", "-m", "sub init"],
        check=True,
        capture_output=True,
    )

    sub_clone = tmp_path / "sub_shallow"
    subprocess.run(
        ["git", "-C", str(work_repo), "checkout", "main"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work_repo), "checkout", "-b", "feature/submodule"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(work_repo),
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(sub_src),
            "ext-tool",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work_repo), "commit", "-m", "add submodule"],
        check=True,
        capture_output=True,
    )
    sub_base = subprocess.check_output(
        ["git", "-C", str(work_repo), "rev-parse", "HEAD"], text=True
    ).strip()
    (sub_src / "README.md").write_text("sub v2\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(sub_src), "commit", "-am", "sub v2"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(work_repo),
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "update",
            "--remote",
            "ext-tool",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work_repo), "commit", "-am", "bump submodule"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work_repo), "push", "-u", "origin", "feature/submodule"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            "feature/submodule",
            f"file://{bare_repo}",
            str(sub_clone),
        ],
        check=True,
        capture_output=True,
    )
    sub_res = subprocess.run(
        [
            "bash",
            "-c",
            script_template.replace("${{ github.workspace }}", str(sub_clone))
            .replace("${{ github.event.pull_request.base.sha }}", sub_base)
            .replace("${{ needs.detect-changes.outputs.has_non_doc }}", "true")
            .replace(
                "uv run --locked python -m tools.reconcile_iac_inputs",
                "printf 'RECONCILE:%s\\n'",
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert sub_res.returncode == 0, sub_res.stderr
    assert "RECONCILE:--changed-file\nRECONCILE:ext-tool" in sub_res.stdout
