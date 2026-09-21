#!/usr/bin/env python3
"""Cross-repo CI gate linter enforcing Left-to-Right testing hierarchy.

Validates GitHub Actions workflow definitions against `tools/ci_spec.py`:
1. Shard bounds: PR matrix shards must be in [SHARD_MIN, SHARD_MAX] (2 to 8) to prevent cold-start bloat.
2. Banned gate steps: heavy benchmarks and full reconciliations must not run in PR gate jobs.
3. Compute saturation: pytest parallelism must use `-n auto` rather than arbitrary hardcoded workers.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from tools.ci_spec import (
    BANNED_IN_GATE_PATTERNS,
    SHARD_MAX,
    SHARD_MIN,
)

HARDCODED_N_WORKERS_RE = re.compile(r"(?:^|\s)-n\s+(\d+)(?:\s|$)")


def _is_pr_gate_workflow(wf: dict[str, Any]) -> bool:
    """Returns True if the workflow triggers on pull_request or merge_group."""
    # PyYAML safe_load may parse `on:` as True if unquoted boolean
    triggers = wf.get(True) or wf.get("on") or {}
    if isinstance(triggers, str):
        return triggers in ("pull_request", "merge_group")
    if isinstance(triggers, list):
        return any(t in ("pull_request", "merge_group") for t in triggers)
    if isinstance(triggers, dict):
        return "pull_request" in triggers or "merge_group" in triggers
    return False


def lint_workflow(path: Path) -> list[str]:
    """Inspects a workflow file for CI gate hierarchy violations."""
    try:
        content = path.read_text(encoding="utf-8")
        wf = yaml.safe_load(content) or {}
    except Exception as exc:
        return [f"{path}: Failed to parse YAML: {exc}"]

    if not isinstance(wf, dict):
        return []

    if not _is_pr_gate_workflow(wf):
        return []

    errors: list[str] = []
    jobs = wf.get("jobs") or {}
    if not isinstance(jobs, dict):
        return []

    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue

        # Check matrix sharding bounds
        strategy = job.get("strategy") or {}
        matrix = strategy.get("matrix") or {}
        if isinstance(matrix, dict):
            shards = matrix.get("shard") or matrix.get("shards") or []
            if isinstance(shards, list) and len(shards) > 1:
                n = len(shards)
                if not (SHARD_MIN <= n <= SHARD_MAX):
                    errors.append(
                        f"{path}:{job_id} matrix shard count {n} violates [{SHARD_MIN}, {SHARD_MAX}]. "
                        f"Consolidate shards with pytest-split / saturated runners."
                    )

        # Check steps for banned patterns and hardcoded worker counts
        steps = job.get("steps") or []
        if isinstance(steps, list):
            for idx, step in enumerate(steps):
                if not isinstance(step, dict):
                    continue
                run_cmd = str(step.get("run") or "").lower()
                step_name = step.get("name") or f"step[{idx}]"

                # Check banned patterns in PR gate
                for pattern in BANNED_IN_GATE_PATTERNS:
                    if pattern in run_cmd:
                        errors.append(
                            f"{path}:{job_id}:{step_name} runs banned gate pattern '{pattern}'. "
                            f"Right-shift heavy benchmarks/reconciliation to L2 nightly workflow."
                        )

                # Check pytest worker hardcoding
                match = HARDCODED_N_WORKERS_RE.search(run_cmd)
                if match:
                    count = match.group(1)
                    errors.append(
                        f"{path}:{job_id}:{step_name} uses hardcoded pytest worker count '-n {count}'. "
                        f"Use '-n auto' to saturate runner CPU without arbitrary magic numbers."
                    )

    return errors


def lint_workflows_dir(workflows_dir: Path) -> list[str]:
    errors: list[str] = []
    files = sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml"))
    for f in files:
        errors.extend(lint_workflow(f))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Lint GitHub Actions workflows for Left-to-Right CI hierarchy.")
    parser.add_argument(
        "--workflows-dir",
        type=Path,
        default=Path(".github/workflows"),
        help="Directory containing workflow YAML files (default: .github/workflows)",
    )
    args = parser.parse_args()

    if not args.workflows_dir.is_dir():
        print(f"Workflow directory not found: {args.workflows_dir}", file=sys.stderr)
        return 0

    errors = lint_workflows_dir(args.workflows_dir)
    if errors:
        print("ci_gate_lint FAILED with violations:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print("ci_gate_lint: All PR workflows satisfy Left-to-Right testing hierarchy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
