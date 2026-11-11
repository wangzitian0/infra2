#!/usr/bin/env python3
"""Find a green pull-request run of infra-ci that tested the same git tree (#1602).

A squash merge onto an unchanged base gives main the tree that the pull request's
merge ref already passed (14 of 20 merges measured on 2026-11-11). For such a push,
the unit-test shards would run the same tests on the same bytes a second time.

The pull-request run uploads the artifact ``unit-green-<tree sha>`` after every gate of
``test-deployer-logic`` passed. For a push of commit ``--commit`` with tree ``--tree``,
this tool prints the id of a run that uploaded that artifact, or prints nothing.
A run counts only when all of these are true:

1. ``--commit`` is the merge commit of a merged pull request (``GET /commits/{sha}/pulls``).
2. The marker artifact has the exact name and is not expired.
3. The run's head repository is this repository (not a fork).
4. The run is a ``pull_request`` run of ``.github/workflows/infra-ci.yml``, and its
   ``head_sha`` is the head of that merged pull request. A run of another pull request
   cannot supply the evidence for this merge, whatever artifacts it uploaded.
5. Every ``Unit tests shard K/N`` job of the run succeeded, and the run still has an
   unexpired ``infra2-coverage-data-shardK`` artifact for each K in 1..N.

Fail safe: an API error, a missing token or any doubt prints nothing, so the shards run.
Stdout carries only the run id; messages go to stderr.
Standard library only: the step runs before any dependency is installed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

MARKER_PREFIX = "unit-green-"
WORKFLOW_PATH = ".github/workflows/infra-ci.yml"
SHARD_ARTIFACT_PREFIX = "infra2-coverage-data-shard"
SHARD_JOB = re.compile(r"^Unit tests shard (\d+)/(\d+)$")
API = "https://api.github.com"
_SHA = re.compile(r"^[0-9a-f]{40}$")

Opener = Callable[..., Any]


def _get(path: str, token: str, opener: Opener) -> Any:
    request = urllib.request.Request(
        f"{API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "infra2-ci-tree-reuse/1.0",
        },
    )
    with opener(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _items(data: Any, key: str) -> list[dict]:
    items = data.get(key) if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError(f"the response has no list of {key}")
    return [i for i in items if isinstance(i, dict)]


def _merged_head(repo: str, commit: str, token: str, opener: Opener) -> str:
    """Head SHA of the merged pull request whose merge commit is ``commit``, or ""."""
    pulls = _get(f"/repos/{repo}/commits/{commit}/pulls", token, opener)
    if not isinstance(pulls, list):
        raise ValueError("the commit pulls response is not a list")
    heads = {
        str((p.get("head") or {}).get("sha") or "")
        for p in pulls
        if isinstance(p, dict)
        and p.get("merge_commit_sha") == commit
        and p.get("merged_at")
    }
    return heads.pop() if len(heads) == 1 else ""


def _all_shards_green_with_coverage(
    repo: str, run_id: int, token: str, opener: Opener
) -> bool:
    jobs = _items(
        _get(f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100", token, opener),
        "jobs",
    )
    shards: dict[int, str] = {}
    totals: set[int] = set()
    for job in jobs:
        match = SHARD_JOB.match(str(job.get("name", "")))
        if match:
            shards[int(match.group(1))] = str(job.get("conclusion"))
            totals.add(int(match.group(2)))
    if len(totals) != 1:
        return False
    total = totals.pop()
    expected = set(range(1, total + 1))
    if set(shards) != expected or any(shards[k] != "success" for k in expected):
        return False
    artifacts = _items(
        _get(
            f"/repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100", token, opener
        ),
        "artifacts",
    )
    live = {str(a.get("name")) for a in artifacts if a.get("expired") is False}
    return all(f"{SHARD_ARTIFACT_PREFIX}{k}" in live for k in expected)


def find_reusable_run(
    repo: str,
    tree: str,
    commit: str,
    token: str,
    *,
    opener: Opener = urllib.request.urlopen,
) -> str:
    """Return the id of a reusable green pull-request run for this push, or ""."""
    if not (repo and token and _SHA.match(tree or "") and _SHA.match(commit or "")):
        return ""
    name = f"{MARKER_PREFIX}{tree}"
    try:
        head = _merged_head(repo, commit, token, opener)
        if not head:
            return ""
        query = urllib.parse.urlencode({"name": name, "per_page": 20})
        markers = _items(
            _get(f"/repos/{repo}/actions/artifacts?{query}", token, opener), "artifacts"
        )
        markers.sort(key=lambda a: str(a.get("created_at") or ""), reverse=True)
        for marker in markers:
            if marker.get("name") != name or marker.get("expired") is not False:
                continue
            origin = marker.get("workflow_run")
            if not isinstance(origin, dict):
                continue
            run_id = origin.get("id")
            repo_id = origin.get("repository_id")
            if (
                not isinstance(run_id, int)
                or repo_id is None
                or repo_id != origin.get("head_repository_id")
            ):
                continue
            run = _get(f"/repos/{repo}/actions/runs/{run_id}", token, opener)
            if (
                not isinstance(run, dict)
                or run.get("path") != WORKFLOW_PATH
                or run.get("event") != "pull_request"
                or run.get("head_sha") != head
            ):
                continue
            if not _all_shards_green_with_coverage(repo, run_id, token, opener):
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
    parser.add_argument("--commit", required=True, help="commit sha of HEAD")
    args = parser.parse_args(argv)
    run_id = find_reusable_run(
        args.repo, args.tree, args.commit, os.environ.get("GITHUB_TOKEN", "")
    )
    if run_id:
        print(run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
