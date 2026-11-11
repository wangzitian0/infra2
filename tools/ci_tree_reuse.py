#!/usr/bin/env python3
"""Find a green pull-request run of infra-ci that tested the same git tree (#1602).

A squash merge onto an unchanged base gives main the tree that the pull request's
merge ref already passed (14 of 20 merges measured on 2026-11-11). For such a push,
the unit-test shards would run the same tests on the same bytes a second time.

The pull-request run uploads the artifact ``unit-green-<tree sha>`` after every gate of
``test-deployer-logic`` passed. This tool prints the id of a run that uploaded that
artifact for ``--tree``, or prints nothing. A run counts only when all of these are true:

1. The marker artifact has the exact name and is not expired.
2. The run's head repository is this repository (not a fork).
3. The run is a ``pull_request`` run of ``.github/workflows/infra-ci.yml``.
4. The run still has at least one unexpired shard coverage artifact.

Fail safe: an API error, a missing token or any doubt prints nothing, so the shards run.
Standard library only: the step runs before any dependency is installed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

MARKER_PREFIX = "unit-green-"
WORKFLOW_PATH = ".github/workflows/infra-ci.yml"
SHARD_ARTIFACT_PREFIX = "infra2-coverage-data-shard"
API = "https://api.github.com"

Opener = Callable[..., Any]


def _get(path: str, token: str, opener: Opener) -> dict:
    request = urllib.request.Request(
        f"{API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "infra2-ci-tree-reuse/1.0",
        },
    )
    with opener(request, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: the response is not a JSON object")
    return data


def _artifacts(data: dict) -> list[dict]:
    items = data.get("artifacts")
    if not isinstance(items, list):
        raise ValueError("the response has no list of artifacts")
    return [a for a in items if isinstance(a, dict)]


def find_reusable_run(
    repo: str, tree: str, token: str, *, opener: Opener = urllib.request.urlopen
) -> str:
    """Return the id of a reusable green pull-request run for ``tree``, or ""."""
    if not (repo and tree and token) or not all(c in "0123456789abcdef" for c in tree):
        return ""
    name = f"{MARKER_PREFIX}{tree}"
    try:
        query = urllib.parse.urlencode({"name": name, "per_page": 20})
        markers = _artifacts(
            _get(f"/repos/{repo}/actions/artifacts?{query}", token, opener)
        )
        markers.sort(key=lambda a: str(a.get("created_at") or ""), reverse=True)
        for marker in markers:
            if marker.get("name") != name or marker.get("expired") is not False:
                continue
            origin = marker.get("workflow_run")
            if not isinstance(origin, dict):
                continue
            repo_id, head_repo_id = (
                origin.get("repository_id"),
                origin.get("head_repository_id"),
            )
            run_id = origin.get("id")
            if (
                not isinstance(run_id, int)
                or repo_id is None
                or repo_id != head_repo_id
            ):
                continue
            run = _get(f"/repos/{repo}/actions/runs/{run_id}", token, opener)
            if run.get("path") != WORKFLOW_PATH or run.get("event") != "pull_request":
                continue
            shards = _artifacts(
                _get(
                    f"/repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100",
                    token,
                    opener,
                )
            )
            if not any(
                str(a.get("name", "")).startswith(SHARD_ARTIFACT_PREFIX)
                and a.get("expired") is False
                for a in shards
            ):
                continue
            return str(run_id)
    except Exception as exc:  # noqa: BLE001 - any failure -> no reuse, the shards run
        print(f"ci_tree_reuse: no reuse ({type(exc).__name__}: {exc})", file=sys.stderr)
        return ""
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--tree", required=True, help="git tree sha of HEAD")
    args = parser.parse_args(argv)
    run_id = find_reusable_run(args.repo, args.tree, os.environ.get("GITHUB_TOKEN", ""))
    if run_id:
        print(run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
