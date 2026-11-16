"""Tests for CI gate linter and Left-to-Right hierarchy rules (#461, #508)."""

from __future__ import annotations

from pathlib import Path
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
