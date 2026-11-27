"""The production promotion guard: it reads stage records and refuses on any doubt (#1646).

``tools/stage_records.py`` is the generic record reader. This module applies it to one
question: may this tag go to production? The `pre` job of the release train calls
``require_upstream_post`` through ``tools/reconcile_iac_inputs.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from common.core._sdk_wire import sdk_version_from_lock_text
from common.core.constants import APP_SOURCES
from common.deploy.canary_pin import (
    APP_SDK_RANGE,
    _version_tuple,
    in_app_sdk_range,
    max_canary_slot_version,
)
from common.deploy.dependencies import matches_service_dependencies
from common.deploy.stage_record import CI_ENV
from common.deploy.stage_record_reader import (
    EXIT_UNREADABLE,
    open_url,
    read_record,
    staging_gates_verdict,
    verified_sdk_versions,
)
from common.deploy.stage_hooks import stage_hook_plan


def required_staging_gates(plan: Any) -> dict[str, str]:
    """``service -> release tag`` of each App or platform/todo release this promotion carries (#1563)."""
    required = {
        svc: tag
        for svc in sorted(plan.selected)
        if svc in APP_SOURCES and (tag := plan.app_release_tags.get(svc, ""))
    }
    canary_ref = str(getattr(plan, "canary_version_ref", "") or "").strip()
    changed_files = getattr(plan, "changed_files", ()) or ()
    platform_or_shared = (
        "platform/todo" in plan.selected
        or matches_service_dependencies("platform/todo", changed_files)
    )
    if canary_ref and platform_or_shared:
        required["platform/todo"] = canary_ref
    return required


def check_prod_canary_health(
    *,
    domain: str = "zitian.party",
    http_get: Callable[[str, float], tuple[int, str]] | None = None,
) -> None:
    """Require live production Canary Todo /api/health and canary verdict to pass."""
    url = f"https://todo.{domain.strip()}/api/health"
    try:
        if http_get is not None:
            status, raw = http_get(url, 10.0)
        else:
            req = urllib.request.Request(
                url, headers={"User-Agent": "infra2-promotion-guard/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                status = int(resp.status)
                raw = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"::error::upstream_post refused: live prod todo canary health failed ({exc})"
        ) from exc
    if status != 200:
        raise SystemExit(
            f"::error::upstream_post refused: live prod todo canary returned HTTP {status}"
        )
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise SystemExit(
            f"::error::upstream_post refused: live prod todo canary returned invalid JSON ({exc})"
        ) from exc
    canary = body.get("canary") if isinstance(body, dict) else None
    if (
        not isinstance(body, dict)
        or body.get("ok") is not True
        or not isinstance(canary, dict)
        or canary.get("ok") is not True
    ):
        raise SystemExit(
            f"::error::upstream_post refused: live prod todo canary unhealthy ({body!r})"
        )


def assert_uv_lock_sdk_verified(
    after: str,
    repo_root: Path,
    *,
    repo: str,
    token: str,
    verified_versions: set[str] | None = None,
) -> None:
    """When ``uv.lock`` exists at ``after``, require its infra2-sdk version to be Canary-verified."""
    shown = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"{after}:uv.lock"],
        capture_output=True,
        text=True,
        check=False,
    )
    if shown.returncode != 0:
        if (repo_root / "uv.lock").is_file():
            raise SystemExit(
                f"::error::upstream_post refused: cannot read {after}:uv.lock ({shown.stderr.strip()})"
            )
        return
    try:
        sdk_ver = sdk_version_from_lock_text(shown.stdout, label=f"{after}:uv.lock")
    except ValueError as exc:
        raise SystemExit(
            f"::error::upstream_post refused: {after}:uv.lock declares no infra2-sdk package"
        ) from exc
    if not in_app_sdk_range(sdk_ver):
        low, high = APP_SDK_RANGE
        raise SystemExit(
            f"::error::upstream_post refused: {after}:uv.lock pins infra2-sdk {sdk_ver}, "
            f"outside range [{low}, {high})"
        )
    if verified_versions is None:
        max_canary = max_canary_slot_version(repo_root)
        if max_canary is not None and _version_tuple(sdk_ver) > _version_tuple(
            max_canary
        ):
            raise SystemExit(
                f"::error::upstream_post refused: {after}:uv.lock pins infra2-sdk {sdk_ver}, "
                f"newer than active Canary slots (max={max_canary})"
            )
    verified = (
        set(verified_versions)
        if verified_versions is not None
        else verified_sdk_versions(
            repo=repo, token=token, opener=open_url, wanted={sdk_ver}
        )
    )
    if sdk_ver not in verified:
        raise SystemExit(
            f"::error::upstream_post refused: {after}:uv.lock pins infra2-sdk {sdk_ver}, "
            f"which has no unrevoked Canary staging_gate pass (verified={sorted(verified)})"
        )


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
    domain = (env.get("INTERNAL_DOMAIN") or "zitian.party").strip()
    if "platform/todo" not in required:
        check_prod_canary_health(domain=domain)
    assert_uv_lock_sdk_verified(after, repo_root, repo=repo, token=token)
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
