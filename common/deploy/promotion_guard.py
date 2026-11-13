"""The production promotion guard: it reads stage records and refuses on any doubt (#1646).

``tools/stage_records.py`` is the generic record reader. This module applies it to one
question: may this tag go to production? The `pre` job of the release train calls
``require_upstream_post`` through ``tools/reconcile_iac_inputs.py``.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from common.core.constants import APP_SOURCES
from common.deploy.stage_record import CI_ENV
from common.deploy.stage_record_reader import (
    EXIT_UNREADABLE,
    read_record,
    staging_gates_verdict,
)
from infra2_sdk.manifests import stage_hook_plan


def required_staging_gates(plan: Any) -> dict[str, str]:
    """``service -> release tag`` of each App whose release this promotion carries (#1563)."""
    return {
        svc: tag
        for svc in sorted(plan.selected)
        if svc in APP_SOURCES and (tag := plan.app_release_tags.get(svc, ""))
    }


def require_upstream_post(
    after: str,
    repo_root: Path,
    *,
    before: str,
    required: Mapping[str, str],
    releases: Callable[[str, str, Path], list[str]],
    env: Mapping[str, str] = os.environ,
) -> None:
    """Refuse a production promotion unless the records of the stage before are green (#1646).

    Three facts, each read from a post record and none from a run listing: the staging soak of
    this tag (a tag-push run, tag on main), the main CI run of the tag commit, and a green
    ``staging_gate`` of every App release since the last production marker (#1563). The check
    runs in the ``pre`` job, before the owner is asked. It prints each reading, so a refusal
    names the failed check id, and ends with ``SystemExit`` on any doubt.
    """
    repo = env.get("GITHUB_REPOSITORY", "")
    token = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN", "")
    if not (repo and token):
        raise SystemExit(
            "::error::upstream_post: GITHUB_REPOSITORY and GITHUB_TOKEN are required"
        )
    commit = subprocess.run(
        ["git", "rev-parse", "--verify", f"{after}^{{commit}}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    staging_env = stage_hook_plan("staging").stage_id

    def staging(tag: str):
        return read_record(
            repo,
            token,
            env=staging_env,
            subject=f"tag:{tag}",
            phase="post",
            event="push",
        )

    readings = {
        "staging soak": staging(after),
        "main CI": read_record(
            repo, token, env=CI_ENV, subject=f"sha:{commit}", phase="post", event="push"
        ),
    }
    required = dict(required) if before else {}
    window: list[dict] = []
    if required:
        for tag in releases(before, after, repo_root):
            reading = readings["staging soak"] if tag == after else staging(tag)
            if reading.exit_code == EXIT_UNREADABLE:
                readings[f"staging soak {tag}"] = reading
            elif reading.ok and reading.record:
                window.append(reading.record)
    refusals = []
    for name, reading in readings.items():
        print(f"upstream_post: {name}: {reading.detail}")
        if not reading.ok:
            refusals.append(f"{name}: {reading.detail}")
    if required and not refusals:
        gates_ok, gates = staging_gates_verdict(window, required)
        print(f"upstream_post: staging_gate since {before}: {gates}")
        if not gates_ok:
            refusals.append(f"staging_gate: {gates}")
    if refusals:
        raise SystemExit("::error::upstream_post refused: " + " | ".join(refusals))
